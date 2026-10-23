"""D1 음성 명령 녹음 웹 애플리케이션 (bench/d1_recorder.py).

용도:
    bench/intent_wav_script_ko.tsv에 수록된 200문장을 사용자가 웹 브라우저에서
    한 줄씩 보며 마이크로 16kHz 모노 16bit WAV로 녹음하여 bench/results/intent_wav/에
    파일명 그대로 저장할 수 있도록 지원하는 개발 전용 녹음 서버.

기능:
    - 200문장 순차/선택 진행 (진행률 n/200 표시)
    - 녹음 / 다시 / 다음 버튼 및 스페이스바 키보드 단축키
    - 브라우저 Web Audio API 기반 16kHz 모노 16bit Linear PCM WAV 인코딩
    - 서버 측 WAV 형식(16kHz/1ch/16bit) 검증 및 경로 탈출(path traversal) 차단
    - 이미 녹음된 파일 상태 표시, 건너뛰기 및 다시 듣기(재생)
"""

from __future__ import annotations

import argparse
from datetime import datetime
import io
import os
from pathlib import Path
import ssl
import sys
from typing import Any
import wave

from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from bench.intent_eval import WavTestCase, load_wav_script


def validate_wav_filename(filename: str, allowed_filenames: set[str] | None = None) -> str:
    """WAV 파일명 무결성 및 경로 탈출(path traversal) 시도 차단."""
    if not filename:
        raise ValueError("파일명이 비어 있습니다.")

    # 1. 경로 구분자 및 상위 디렉터리 접근 차단
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise ValueError("경로 탈출(path traversal) 시도가 감지되었습니다.")

    # 2. 파일명 확장자 검사
    if not filename.endswith(".wav"):
        raise ValueError("WAV 파일(.wav)만 허용됩니다.")

    # 3. 순수 파일명(basename) 검증
    clean = Path(filename).name
    if clean != filename:
        raise ValueError("잘못된 파일명 형식입니다.")

    # 4. 허용 목록(대본에 정의된 파일명) 검사
    if allowed_filenames is not None and clean not in allowed_filenames:
        raise ValueError(f"대본에 정의되지 않은 파일명입니다: {clean}")

    return clean


def validate_wav_bytes(pcm_bytes: bytes) -> tuple[int, int, int]:
    """16kHz 모노 16bit Linear PCM WAV 규격 유효성 검사. (channels, sampwidth, framerate) 반환."""
    if len(pcm_bytes) < 44:
        raise ValueError(f"WAV 헤더 크기 부족 ({len(pcm_bytes)} 바이트)")

    try:
        with wave.open(io.BytesIO(pcm_bytes), "rb") as w:
            ch = w.getnchannels()
            sw = w.getsampwidth()
            sr = w.getframerate()
    except Exception as e:
        raise ValueError(f"올바른 WAV 형식이 아닙니다: {e}")

    if ch != 1:
        raise ValueError(f"모노(1채널)만 허용됩니다 (현재: {ch}채널)")
    if sw != 2:
        raise ValueError(f"16비트 PCM만 허용됩니다 (현재: {sw*8}비트)")
    if sr != 16000:
        raise ValueError(f"16,000 Hz 샘플레이트만 허용됩니다 (현재: {sr} Hz)")

    return ch, sw, sr


INDEX_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HealthUp D1 음성 명령 녹음기</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --card-hover: #334155;
    --fg: #f8fafc; --fg-muted: #94a3b8; --line: #334155;
    --accent: #38bdf8; --accent-hover: #0284c7;
    --danger: #ef4444; --danger-hover: #dc2626;
    --success: #10b981; --warn: #f59e0b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--fg); min-height: 100vh; display: flex; flex-direction: column; align-items: center;
  }
  header { width: 100%; max-width: 860px; margin-bottom: 16px; }
  h1 { margin: 0 0 6px; font-size: 20px; display: flex; align-items: center; gap: 8px; }
  .desc { margin: 0; color: var(--fg-muted); font-size: 13px; }
  .card {
    width: 100%; max-width: 860px; background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 4px 16px rgba(0,0,0,0.25);
  }
  .progress-wrap { margin-bottom: 20px; }
  .progress-header { display: flex; justify-content: space-between; font-size: 13px; color: var(--fg-muted); margin-bottom: 6px; }
  .progress-bar { width: 100%; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s; }
  .prompt-box {
    background: #090d16; border: 1px solid var(--line); border-radius: 10px; padding: 24px 20px;
    text-align: center; margin-bottom: 20px; position: relative;
  }
  .prompt-meta { display: flex; justify-content: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
  .badge {
    padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase;
    background: var(--card); color: var(--accent); border: 1px solid var(--line);
  }
  .badge.state { color: var(--warn); border-color: rgba(245, 158, 11, 0.4); }
  .badge.intent { color: var(--accent); border-color: rgba(56, 189, 248, 0.4); }
  .badge.status-ok { background: rgba(16, 185, 129, 0.15); color: var(--success); border-color: var(--success); }
  .badge.status-none { background: rgba(148, 163, 184, 0.1); color: var(--fg-muted); border-color: var(--line); }
  .prompt-text { font-size: 32px; font-weight: 700; color: #fff; margin: 10px 0; word-break: keep-all; line-height: 1.4; }
  .prompt-hint { font-size: 13px; color: var(--fg-muted); margin: 6px 0 0; }
  .meter-wrap { margin: 12px 0 16px; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .meter-fill { height: 100%; width: 0%; background: var(--success); transition: width 0.05s linear; }
  .record-actions { display: flex; justify-content: center; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  button {
    font-size: 14px; font-weight: 600; padding: 10px 18px; border-radius: 8px; border: 1px solid var(--line);
    background: var(--card); color: var(--fg); cursor: pointer; transition: background 0.15s, border-color 0.15s;
    display: inline-flex; align-items: center; gap: 6px; user-select: none;
  }
  button:hover { background: var(--card-hover); border-color: var(--fg-muted); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-rec {
    font-size: 16px; padding: 14px 28px; background: var(--accent); color: #0f172a; border-color: transparent; border-radius: 30px;
  }
  .btn-rec:hover { background: var(--accent-hover); }
  .btn-rec.recording {
    background: var(--danger); color: #fff; animation: pulse 1.2s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.9; transform: scale(1.02); } }
  .nav-actions { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--line); padding-top: 16px; }
  .select-wrap { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  select {
    background: var(--bg); color: var(--fg); border: 1px solid var(--line); border-radius: 6px;
    padding: 6px 10px; font-size: 13px; max-width: 380px;
  }
  .msg-bar { font-size: 12px; color: var(--fg-muted); text-align: center; min-height: 18px; margin-top: 10px; }
  .kbd {
    display: inline-block; padding: 2px 6px; font-size: 11px; background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.2); border-radius: 4px; color: #cbd5e1;
  }
</style>
</head>
<body>

<header>
  <h1>🎙️ HealthUp D1 음성 명령 녹음기 <span style="font-size:12px; font-weight:normal; color:var(--accent);">포트 8791</span></h1>
  <p class="desc">16kHz 모노 16bit WAV 녹음 · 스페이스바로 시작/종료 · 저장 경로: <code>bench/results/intent_wav/</code></p>
</header>

<main class="card">
  <div class="progress-wrap">
    <div class="progress-header">
      <span id="progressText">녹음 진행률: 0 / 200 (0.0%)</span>
      <span id="fileCountBadge">남은 문장: 200개</span>
    </div>
    <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
  </div>

  <div class="prompt-box">
    <div class="prompt-meta">
      <span class="badge" id="badgeIndex">#001 / 200</span>
      <span class="badge state" id="badgeState">상태: idle</span>
      <span class="badge intent" id="badgeIntent">의도: SESSION_START</span>
      <span class="badge" id="badgeFilename">001_SESSION_START_1_r1.wav</span>
      <span class="badge status-none" id="badgeStatus">미녹음</span>
    </div>

    <div class="prompt-text" id="promptText">시작하자</div>
    <div class="prompt-hint" id="promptHint">대기 상태에서 기기를 깨우듯 명확하게 발화하세요.</div>
  </div>

  <div class="meter-wrap"><div id="meterFill" class="meter-fill"></div></div>

  <div class="record-actions">
    <button id="btnRecord" class="btn-rec">⏺️ 녹음 시작 <span class="kbd">Space</span></button>
    <button id="btnPlay" disabled>▶️ 다시 듣기 <span class="kbd">P</span></button>
    <button id="btnDelete" disabled style="color:var(--danger)">🗑️ 파일 삭제</button>
  </div>

  <div class="nav-actions">
    <div style="display:flex; gap:8px;">
      <button id="btnPrev">◀️ 이전 <span class="kbd">←</span></button>
      <button id="btnNext">다음 ▶️ <span class="kbd">→</span></button>
      <button id="btnNextUnrecorded" style="color:var(--accent)">⏭️ 다음 미녹음 문장</button>
    </div>

    <div class="select-wrap">
      <label for="itemSelect">이동:</label>
      <select id="itemSelect"></select>
    </div>
  </div>

  <div class="msg-bar" id="msgBar">마이크 권한을 허용하고 스페이스바를 눌러 녹음을 시작하세요.</div>
</main>

<audio id="audioPlayer" style="display:none;"></audio>

<script>
let items = [];
let currentIndex = 0;
let isRecording = false;
let audioCtx = null;
let micStream = null;
let processor = null;
let source = null;
let recordedChunks = [];
let recordingStartTime = 0;
let timerInterval = null;

const $ = id => document.getElementById(id);

async function init() {
  try {
    const res = await fetch("/api/items");
    items = await res.json();
    populateSelect();
    updateUI();
  } catch (err) {
    $("msgBar").textContent = "대본 목록 로드 실패: " + err;
  }
}

function populateSelect() {
  const sel = $("itemSelect");
  sel.innerHTML = "";
  items.forEach((it, idx) => {
    const opt = document.createElement("option");
    opt.value = idx;
    opt.textContent = `${it.exists ? "✅" : "⬜"} [${it.index + 1}] ${it.filename} — ${it.text}`;
    sel.appendChild(opt);
  });
  sel.onchange = (e) => {
    currentIndex = parseInt(e.target.value, 10);
    updateUI();
  };
}

function updateUI() {
  if (!items.length) return;
  const it = items[currentIndex];

  $("badgeIndex").textContent = `#${String(it.index + 1).padStart(3, '0')} / ${items.length}`;
  $("badgeState").textContent = `상태: ${it.state}`;
  $("badgeIntent").textContent = `의도: ${it.intent}`;
  $("badgeFilename").textContent = it.filename;
  $("promptText").textContent = it.text;

  // 상태별 안내 힌트
  let hint = "보통 어조와 속도로 자연스럽게 발화하세요.";
  if (it.state === "idle") hint = "대기 상태에서 기기를 깨우듯 또박또박 발화하세요.";
  else if (it.state === "speaking") hint = "기기가 말하는 도중에 끊듯 발화하세요 (정지 명령).";
  else if (it.state === "confirming") hint = "확인 질문에 대답하는 어조로 발화하세요 (네/아니요).";
  else if (it.intent === "CRISIS") hint = "위기 상황을 가정한 연기 어조로 발화하세요.";
  $("promptHint").textContent = hint;

  if (it.exists) {
    $("badgeStatus").textContent = "✅ 녹음 완료";
    $("badgeStatus").className = "badge status-ok";
    $("btnPlay").disabled = false;
    $("btnDelete").disabled = false;
  } else {
    $("badgeStatus").textContent = "미녹음";
    $("badgeStatus").className = "badge status-none";
    $("btnPlay").disabled = true;
    $("btnDelete").disabled = true;
  }

  $("itemSelect").value = currentIndex;

  const recordedCount = items.filter(x => x.exists).length;
  const pct = (recordedCount / items.length * 100).toFixed(1);
  $("progressText").textContent = `녹음 진행률: ${recordedCount} / ${items.length} (${pct}%)`;
  $("fileCountBadge").textContent = `남은 문장: ${items.length - recordedCount}개`;
  $("progressFill").style.width = pct + "%";
}

async function startRecording() {
  if (isRecording) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 16000, echoCancellation: false, noiseSuppression: false }
    });
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    source = audioCtx.createMediaStreamSource(micStream);
    processor = audioCtx.createScriptProcessor(4096, 1, 1);

    recordedChunks = [];
    processor.onaudioprocess = (e) => {
      if (!isRecording) return;
      const ch = e.inputBuffer.getChannelData(0);
      recordedChunks.push(new Float32Array(ch));

      // 레벨 미터
      let sum = 0;
      for (let i = 0; i < ch.length; i += 8) sum += ch[i] * ch[i];
      const rms = Math.sqrt(sum / (ch.length / 8));
      const lvl = Math.min(100, Math.round(rms * 400));
      $("meterFill").style.width = lvl + "%";
    };

    source.connect(processor);
    processor.connect(audioCtx.destination);

    isRecording = true;
    recordingStartTime = Date.now();
    $("btnRecord").className = "btn-rec recording";
    $("btnRecord").innerHTML = "⏹️ 녹음 중지 & 저장 <span class='kbd'>Space</span>";
    $("msgBar").textContent = "🎙️ 녹음 중... 말씀이 끝나면 스페이스바를 다시 누르세요.";

    timerInterval = setInterval(() => {
      const sec = ((Date.now() - recordingStartTime) / 1000).toFixed(1);
      $("btnRecord").innerHTML = `⏹️ [${sec}초] 중지 & 저장 <span class='kbd'>Space</span>`;
    }, 100);
  } catch (err) {
    alert("마이크 접근 실패: " + err.message);
  }
}

async function stopRecording() {
  if (!isRecording) return;
  isRecording = false;
  clearInterval(timerInterval);
  $("meterFill").style.width = "0%";
  $("btnRecord").className = "btn-rec";
  $("btnRecord").innerHTML = "⏺️ 녹음 시작 <span class='kbd'>Space</span>";

  if (processor) processor.disconnect();
  if (source) source.disconnect();
  if (micStream) micStream.getTracks().forEach(t => t.stop());

  let totalLen = 0;
  for (const c of recordedChunks) totalLen += c.length;
  if (totalLen < 3200) { // 0.2초 미만 무효 처리
    $("msgBar").textContent = "⚠️ 발화 길이가 너무 짧아(0.2초 미만) 저장하지 않았습니다.";
    return;
  }

  const merged = new Float32Array(totalLen);
  let offset = 0;
  for (const c of recordedChunks) {
    merged.set(c, offset);
    offset += c.length;
  }

  // 16kHz 리샘플 (필요 시)
  const actualSr = audioCtx.sampleRate;
  const pcm16 = (actualSr === 16000) ? merged : resampleLinear(merged, actualSr, 16000);
  const wavBlob = encodeWav16kMono(pcm16);

  const it = items[currentIndex];
  $("msgBar").textContent = `저장 중: ${it.filename}...`;

  try {
    const res = await fetch(`/api/upload?filename=${encodeURIComponent(it.filename)}`, {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wavBlob
    });
    const data = await res.json();
    if (data.ok) {
      it.exists = true;
      populateSelect();
      updateUI();
      $("msgBar").textContent = `✅ 저장 완료: ${it.filename} (${(pcm16.length / 16000).toFixed(1)}초, ${Math.round(wavBlob.size / 1024)} KB)`;

      // 다음 항목으로 자동 이동
      if (currentIndex < items.length - 1) {
        setTimeout(() => {
          currentIndex++;
          updateUI();
        }, 300);
      }
    } else {
      $("msgBar").textContent = `❌ 저장 실패: ${data.error || "알 수 없는 오류"}`;
    }
  } catch (err) {
    $("msgBar").textContent = `❌ 업로드 네트워크 오류: ${err.message}`;
  }
}

function resampleLinear(samples, inRate, outRate) {
  const ratio = inRate / outRate;
  const newLen = Math.round(samples.length / ratio);
  const result = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const origIdx = i * ratio;
    const i0 = Math.floor(origIdx);
    const i1 = Math.min(i0 + 1, samples.length - 1);
    const frac = origIdx - i0;
    result[i] = samples[i0] * (1 - frac) + samples[i1] * frac;
  }
  return result;
}

function encodeWav16kMono(samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  function writeStr(offset, str) {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  }

  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // Mono
  view.setUint32(24, 16000, true); // 16kHz
  view.setUint32(28, 32000, true); // byteRate = 16000 * 2
  view.setUint16(32, 2, true); // blockAlign
  view.setUint16(34, 16, true); // 16bit
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }

  return new Blob([buffer], { type: "audio/wav" });
}

function playAudio() {
  const it = items[currentIndex];
  if (!it || !it.exists) return;
  const player = $("audioPlayer");
  player.src = `/api/audio/${encodeURIComponent(it.filename)}?t=${Date.now()}`;
  player.play();
  $("msgBar").textContent = `▶️ 재생 중: ${it.filename}`;
}

async function deleteAudio() {
  const it = items[currentIndex];
  if (!it || !it.exists) return;
  if (!confirm(`정말 ${it.filename} 파일을 삭제하고 다시 녹음하시겠습니까?`)) return;

  try {
    const res = await fetch(`/api/audio/${encodeURIComponent(it.filename)}`, { method: "DELETE" });
    const data = await res.json();
    if (data.ok) {
      it.exists = false;
      populateSelect();
      updateUI();
      $("msgBar").textContent = `🗑️ 파일 삭제 완료: ${it.filename}`;
    }
  } catch (err) {
    $("msgBar").textContent = `삭제 실패: ${err.message}`;
  }
}

function findNextUnrecorded() {
  for (let i = currentIndex + 1; i < items.length; i++) {
    if (!items[i].exists) {
      currentIndex = i;
      updateUI();
      return;
    }
  }
  for (let i = 0; i <= currentIndex; i++) {
    if (!items[i].exists) {
      currentIndex = i;
      updateUI();
      return;
    }
  }
  $("msgBar").textContent = "🎉 축하합니다! 200문장 모두 녹음이 완료되었습니다!";
}

$("btnRecord").onclick = () => isRecording ? stopRecording() : startRecording();
$("btnPlay").onclick = playAudio;
$("btnDelete").onclick = deleteAudio;
$("btnPrev").onclick = () => { if (currentIndex > 0) { currentIndex--; updateUI(); } };
$("btnNext").onclick = () => { if (currentIndex < items.length - 1) { currentIndex++; updateUI(); } };
$("btnNextUnrecorded").onclick = findNextUnrecorded;

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.code === "Space") {
    e.preventDefault();
    isRecording ? stopRecording() : startRecording();
  } else if (e.code === "ArrowLeft") {
    e.preventDefault();
    if (currentIndex > 0) { currentIndex--; updateUI(); }
  } else if (e.code === "ArrowRight") {
    e.preventDefault();
    if (currentIndex < items.length - 1) { currentIndex++; updateUI(); }
  } else if (e.code === "KeyP") {
    e.preventDefault();
    playAudio();
  }
});

init();
</script>
</body>
</html>
"""


def create_app(script_path: Path, intent_wav_dir: Path) -> web.Application:
    """녹음기 aiohttp 애플리케이션 생성."""
    intent_wav_dir.mkdir(parents=True, exist_ok=True)
    cases = load_wav_script(script_path)
    allowed_filenames = {c.wav_filename for c in cases}

    app = web.Application()

    async def handle_index(request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def handle_get_items(request: web.Request) -> web.Response:
        result = []
        for i, c in enumerate(cases):
            p = intent_wav_dir / c.wav_filename
            exists = p.is_file()
            size = p.stat().st_size if exists else 0
            result.append({
                "index": i,
                "text": c.text,
                "intent": c.expected_intent,
                "filename": c.wav_filename,
                "repeat": c.repeat,
                "state": c.eval_state,
                "exists": exists,
                "size": size,
            })
        return web.json_response(result)

    async def handle_upload(request: web.Request) -> web.Response:
        filename = request.query.get("filename")
        if not filename:
            return web.json_response({"ok": False, "error": "filename 파라미터가 누락되었습니다."}, status=400)

        try:
            clean_name = validate_wav_filename(filename, allowed_filenames)
        except ValueError as e:
            return web.json_response({"ok": False, "error": f"파일명 거부: {e}"}, status=400)

        body = await request.read()
        try:
            validate_wav_bytes(body)
        except ValueError as e:
            return web.json_response({"ok": False, "error": f"WAV 형식 거부: {e}"}, status=400)

        target_path = intent_wav_dir / clean_name
        target_path.write_bytes(body)
        return web.json_response({"ok": True, "filename": clean_name, "size": len(body)})

    async def handle_get_audio(request: web.Request) -> web.Response:
        filename = request.query.get("filename") or request.match_info.get("filename", "")
        if not filename:
            return web.Response(text="filename 파라미터가 필요합니다.", status=400)
        try:
            clean_name = validate_wav_filename(filename, allowed_filenames)
        except ValueError as e:
            return web.Response(text=f"파일명 거부: {e}", status=400)

        target_path = intent_wav_dir / clean_name
        if not target_path.is_file():
            return web.Response(text="파일이 없습니다.", status=404)
        return web.FileResponse(target_path)

    async def handle_delete_audio(request: web.Request) -> web.Response:
        filename = request.query.get("filename") or request.match_info.get("filename", "")
        if not filename:
            return web.json_response({"ok": False, "error": "filename 파라미터가 필요합니다."}, status=400)
        try:
            clean_name = validate_wav_filename(filename, allowed_filenames)
        except ValueError as e:
            return web.json_response({"ok": False, "error": f"파일명 거부: {e}"}, status=400)

        target_path = intent_wav_dir / clean_name
        if target_path.is_file():
            target_path.unlink()
        return web.json_response({"ok": True, "filename": clean_name})

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/items", handle_get_items)
    app.router.add_post("/api/upload", handle_upload)
    app.router.add_get("/api/audio", handle_get_audio)
    app.router.add_get("/api/audio/{filename}", handle_get_audio)
    app.router.add_delete("/api/audio", handle_delete_audio)
    app.router.add_delete("/api/audio/{filename}", handle_delete_audio)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HealthUp D1 음성 명령 녹음 서버 (포트 8791)")
    parser.add_argument("--host", default="0.0.0.0", help="바인딩 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8791, help="청취 포트 (기본: 8791)")
    parser.add_argument("--script", default="bench/intent_wav_script_ko.tsv", help="대본 TSV 파일 경로")
    parser.add_argument("--wav-dir", default="bench/results/intent_wav", help="녹음 파일 저장 디렉터리")
    parser.add_argument("--cert", default=None, help="SSL/TLS 인증서 파일 경로 (HTTPS 필요 시)")
    parser.add_argument("--key", default=None, help="SSL/TLS 개인키 파일 경로 (HTTPS 필요 시)")
    args = parser.parse_args()

    script_path = Path(args.script)
    wav_dir = Path(args.wav_dir)

    if not script_path.exists():
        print(f"오류: 녹음 대본 파일이 없습니다: {script_path}", file=sys.stderr)
        sys.exit(2)

    app = create_app(script_path, wav_dir)

    ssl_ctx = None
    if args.cert and args.key:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
        protocol = "https"
    else:
        protocol = "http"

    print("=" * 70)
    print("  HealthUp D1 음성 명령 녹음 서버 기동")
    print(f"  대본: {script_path}")
    print(f"  저장 폴더: {wav_dir}")
    print(f"  접속 주소: {protocol}://127.0.0.1:{args.port}/")
    print("=" * 70)

    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx)


if __name__ == "__main__":
    main()
