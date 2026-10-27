"use strict";
const $ = (id) => document.getElementById(id);
const JITTER = 0.06;        // 초기 지터 버퍼 (초)
const PROGRESS_MS = 100;    // 재생 위치 보고 주기
const DUCK_GAIN = 0.25;     // 사용자가 말하기 시작하면 낮추는 음량 (덕킹)

let ws = null, ctx = null, master = null;
let workletLoaded = false;
let micStream = null, micNode = null, running = false;
let currentSessionState = "idle";
let faceStream = null, faceIntervalId = null;
let faceFrames = [];
let totalSpeechFrames = 0;
let faceConsented = !!sessionStorage.getItem("healthup_face_consent");
let faceModelLoading = false;
let faceModelFailed = false;
let faceLandmarksReady = false;
let faceLandmarkMissing = false;
let faceAnalyzing = false;
let faceCameraState = "대기";
let faceCameraError = "";
let faceFpsMarks = [];
let lastFaceSent = null;
let lastFaceSentAt = "";
let faceHudLive = { detected: false, score: null, faceCount: 0, emotions: [], fps: null };
let cur = null;             // 현재 재생 중인 턴
const deadTurns = new Set();
let wantRun = false;        // 「시작」을 눌러 실행 중 (끊기면 재연결한다)
let retries = 0, retryTimer = null;
const TOKEN = new URLSearchParams(location.search).get("t");  // 인증 토큰 (?t=)
let speakingLocal = false, lastState = "listening";
const turnEls = {};         // turn_id -> 말풍선
const latRows = {};

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
window.send = send;

// ---------- 모드 및 환경설정 (A2) ----------
const DEFAULT_PREFS = {
  wake: true,
  face: false,
  show_ai_notice: true,
  show_camera_badge: true,
  show_conn_status: true,
  show_dev_info: false
};

let currentMode = localStorage.getItem("healthup_mode") || "demo";
let prefs = Object.assign({}, DEFAULT_PREFS);
try {
  const saved = JSON.parse(localStorage.getItem("healthup_prefs") || "{}");
  Object.assign(prefs, saved);
  // 세션 단위 동의(sessionStorage): 미동의 상태에서는 face 강제 false (§4.1, §4.4)
  if (!faceConsented) {
    prefs.face = false;
  }
} catch (e) {}

function savePrefs() {
  localStorage.setItem("healthup_prefs", JSON.stringify(prefs));
  applyVisibility();
}

function setMode(newMode, notifyServer = true) {
  if (newMode !== "demo" && newMode !== "dev") return;
  currentMode = newMode;
  localStorage.setItem("healthup_mode", currentMode);
  if ($("modeDemo")) $("modeDemo").checked = (currentMode === "demo");
  if ($("modeDev")) $("modeDev").checked = (currentMode === "dev");
  applyVisibility();
  if (notifyServer && ws && ws.readyState === 1) {
    send({ type: "config", mode: currentMode });
  }
}

function applyVisibility() {
  // 1. AI 마음 건강 도우미 고지
  if ($("aiNotice")) $("aiNotice").hidden = !prefs.show_ai_notice;
  // 2. 카메라 / 대기 마이크 상태 배지
  if ($("cameraBadge")) {
    const parts = [];
    if (prefs.face) parts.push("카메라 켜짐");
    if (prefs.wake) parts.push("마이크 대기 중");
    $("cameraBadge").textContent = parts.join(" · ") || "마이크 대기 중";
    $("cameraBadge").hidden = !prefs.show_camera_badge || parts.length === 0;
  }
  // 3. 연결 상태 표시
  if ($("connStatus")) $("connStatus").hidden = !prefs.show_conn_status;

  // 4. 개발 패널 vs 데모 자리표시 카드 표시
  const showDev = (currentMode === "dev" || prefs.show_dev_info);
  if ($("micCard")) $("micCard").hidden = !showDev;
  if ($("textCard")) $("textCard").hidden = !showDev;
  if ($("logCard")) $("logCard").hidden = !showDev;
  applyTalkLayout(showDev);
  if ($("latCard")) $("latCard").hidden = !showDev;
  if ($("epCard") && !showDev) $("epCard").hidden = true;

  // 헤더 개발 정보 및 옵션 선택기 제어
  if ($("engines")) $("engines").hidden = !showDev;
  if ($("voiceWrap")) $("voiceWrap").hidden = !showDev || !$("voice") || $("voice").options.length === 0;
  if ($("sttWrap")) $("sttWrap").hidden = !showDev || !$("stt") || $("stt").options.length === 0;
  if ($("epWrap")) $("epWrap").hidden = !showDev || !$("endpoint") || $("endpoint").options.length === 0;

  // 자리표시 영역 (데모 모드 기본 표시, 개발 모드에서는 show_dev_info 미설정 시 숨김)
  if ($("avatarCard")) {
    $("avatarCard").hidden = (currentMode === "dev" && !prefs.show_dev_info);
  }

  // 체크박스 상태 동기화
  if ($("prefWake")) $("prefWake").checked = !!prefs.wake;
  if ($("prefFace")) $("prefFace").checked = !!prefs.face;
  if ($("prefAiNotice")) $("prefAiNotice").checked = !!prefs.show_ai_notice;
  if ($("prefCameraBadge")) $("prefCameraBadge").checked = !!prefs.show_camera_badge;
  if ($("prefConnStatus")) $("prefConnStatus").checked = !!prefs.show_conn_status;
  if ($("prefDevInfo")) $("prefDevInfo").checked = !!prefs.show_dev_info;

  renderFaceHud();
}

function applyTalkLayout(showLog) {
  const hud = window.FaceHud;
  const layout = hud && hud.talkLayout
    ? hud.talkLayout(!!prefs.face, currentMode)
    : { showCamera: !!prefs.face, showOverlay: false, showStatus: false, columns: 1 };
  if ($("faceStage")) $("faceStage").hidden = !layout.showCamera;
  if ($("faceOverlay")) $("faceOverlay").hidden = !layout.showOverlay;
  if ($("faceHud")) $("faceHud").hidden = !layout.showStatus;
  if ($("talkStage")) {
    $("talkStage").hidden = !showLog && !layout.showCamera;
    $("talkStage").classList.toggle("split", !!(layout.showCamera && showLog));
  }
}

// ---------- UI ----------
let epBadge = null;
function setBadge() {
  const names = { listening: "듣는 중", thinking: "생각 중", speaking: "말하는 중", safety: "안전 안내" };
  const st = speakingLocal && lastState !== "safety" ? "speaking" : lastState;
  const label = epBadge ? epBadge.text : (names[st] || st);
  const cls = epBadge ? epBadge.cls : st;

  $("badge").textContent = label;
  $("badge").className = "badge " + cls;

  if ($("avatarStatusText")) {
    $("avatarStatusText").textContent = names[st] || st;
    $("avatarStatusText").className = "avatar-status " + st;
  }
}

function addMsg(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.textContent = text;
  $("log").appendChild(d); $("log").scrollTop = $("log").scrollHeight;
  return d;
}
function fmt(v) { return v == null ? "-" : Math.round(v); }
const traces = window.traces = {};

function calcPercentile(values, p) {
  if (!values || !values.length) return null;
  const xs = values.slice().sort((a, b) => a - b);
  if (xs.length === 1) return xs[0];
  const k = (xs.length - 1) * p / 100.0;
  const lo = Math.floor(k), hi = Math.min(lo + 1, xs.length - 1);
  return xs[lo] + (xs[hi] - xs[lo]) * (k - lo);
}

function updateLatSummary() {
  const metricKeys = ["stt_ms", "turn_confirmed", "llm_first_token", "first_chunk", "backchannel", "tts_first_audio", "playback_start"];
  const turns = Object.values(traces);
  const n = turns.length;
  if ($("latSumLabel")) $("latSumLabel").textContent = `P50 / P90 (${n}턴)`;
  for (const key of metricKeys) {
    const vals = turns.map(t => t[key]).filter(v => typeof v === "number" && !isNaN(v));
    const el = $("latSum_" + key);
    if (!el) continue;
    if (!vals.length) {
      el.textContent = "-";
    } else {
      const p50 = calcPercentile(vals, 50);
      const p90 = calcPercentile(vals, 90);
      el.textContent = `${Math.round(p50)} / ${Math.round(p90)}`;
    }
  }
}

function setLatency(turn_id, upd) {
  const tr = traces[turn_id] = traces[turn_id] || {};
  for (const k in upd) if (upd[k] != null) tr[k] = upd[k]; // 늦게 오는 값(playback_start)만 채운다
  let tr_el = latRows[turn_id];
  if (!tr_el) { tr_el = latRows[turn_id] = document.createElement("tr"); $("lat").prepend(tr_el); }
  const cells = [`#${turn_id}`, tr.stt_ms, tr.turn_confirmed, tr.llm_first_token, tr.first_chunk, tr.backchannel, tr.tts_first_audio, tr.playback_start];
  tr_el.innerHTML = cells.map((c, i) => `<td>${i ? fmt(c) : c}</td>`).join("");
  updateLatSummary();
}

// ---------- 턴 종료 판단 (smart) ----------
const EP_LABEL = { waiting: "듣는 중", resumed: "말 이어짐", reopened: "말 이어짐", confirmed: "턴 확정", dropped: "턴 버림" };
let ep = null, epRaf = 0;   // 현재 대기: {total, base, t0}
function epTick() {
  if (!ep) return;
  const left = Math.max(0, ep.total - (ep.base + performance.now() - ep.t0));
  $("epFill").style.width = (ep.total ? (left / ep.total) * 100 : 0) + "%";
  $("epWait").textContent = `남은 대기 ${Math.round(left)}ms / ${ep.total}ms`;
  epRaf = left > 0 ? requestAnimationFrame(epTick) : 0;
}
function onTurnEval(m) {
  const label = EP_LABEL[m.stage] || m.stage;
  if (m.stage === "reopened") {
    const us = document.querySelectorAll("#log .msg.user");
    if (us.length) us[us.length - 1].classList.add("merged");
  }
  const p = m.smart_turn_p == null ? "-" : m.smart_turn_p.toFixed(2);
  const e = m.ending || {};
  if (currentMode === "dev" || prefs.show_dev_info) {
    $("epCard").hidden = false;
  }
  $("epP").textContent = `Smart Turn p ${p}`;
  $("epEnd").textContent = `어미 ${e.category || "-"} ${e.morph ? "'" + e.morph + "'" : ""}`;
  $("epStt").textContent = `STT ${fmt(m.stt_ms)}ms${m.stt_punct ? " (문장부호)" : ""}`;
  cancelAnimationFrame(epRaf);
  if (m.stage === "waiting") {
    ep = { total: m.wait_ms, base: m.since_pause_ms, t0: performance.now() };
    epTick();
  } else {
    ep = null; $("epFill").style.width = "0";
    $("epWait").textContent = m.stage === "resumed" ? "대기 취소 (말 이어짐)" : m.stage === "reopened" ? "확정 취소 (이어 붙임)" : m.stage === "confirmed" ? "확정" : "버림";
  }
  epBadge = m.stage === "dropped" ? null
    : { text: `${label} (사유: ${m.reason})`, cls: m.stage === "confirmed" ? "thinking" : "listening" };
  setBadge();
  addMsg("judge", `[판단] ${label} — ${m.reason} · ST ${p} · "${m.text}"`);
}

// ---------- 재생 (브라우저가 Unreal 역할) ----------
function playedMs(t) {
  const now = ctx.currentTime;
  while (t.ptr < t.chunks.length && t.chunks[t.ptr].t + t.chunks[t.ptr].d <= now) {
    t.doneMs += t.chunks[t.ptr].d * 1000; t.ptr++;
  }
  let ms = t.doneMs;
  if (t.ptr < t.chunks.length && now > t.chunks[t.ptr].t) ms += (now - t.chunks[t.ptr].t) * 1000;
  return ms;
}
function stopTimers(t) { clearInterval(t.prog); clearTimeout(t.startTimer); clearTimeout(t.finTimer); }

function onBegin(m) {
  if (master) master.gain.setTargetAtTime(1, ctx.currentTime, 0.01);   // 덕킹 해제
  if (cur && !cur.dead) killTurn(cur, 0, false);
  const gain = ctx.createGain(); gain.connect(master);
  cur = { id: m.turn_id, rate: m.sample_rate, gain, cursor: 0, chunks: [], ptr: 0, doneMs: 0, totalMs: 0,
          sources: [], started: false, dead: false, prog: null, startTimer: null, finTimer: null };
  speakingLocal = true; setBadge();
  if ($("avatarVolWrap")) $("avatarVolWrap").hidden = false;
}
function onFrame(buf) {
  const dv = new DataView(buf);
  const tid = dv.getUint32(0, true);
  if (!cur || cur.dead || cur.id !== tid || deadTurns.has(tid)) return;
  const n = (buf.byteLength - 10) >> 1;
  if (n <= 0) return;
  const pcm = new Int16Array(buf, 10, n);
  const ab = ctx.createBuffer(1, n, cur.rate);
  const f = ab.getChannelData(0);
  for (let i = 0; i < n; i++) f[i] = pcm[i] / 32768;
  const src = ctx.createBufferSource(); src.buffer = ab; src.connect(cur.gain);
  const now = ctx.currentTime;
  let t = cur.cursor;
  if (!cur.started && cur.chunks.length === 0) t = now + JITTER;
  else if (t < now + 0.005) t = now + 0.02;
  src.start(t);
  const d = n / cur.rate;
  cur.cursor = t + d; cur.totalMs += d * 1000;
  cur.chunks.push({ t, d }); cur.sources.push(src);
  if (cur.chunks.length === 1) {
    const turn = cur;
    turn.startTimer = setTimeout(() => {
      if (turn.dead) return;
      turn.started = true;
      send({ type: "playback", turn_id: turn.id, event: "started", played_ms: 0 });
      turn.prog = setInterval(() => {
        send({ type: "playback", turn_id: turn.id, event: "progress", played_ms: playedMs(turn) });
        if ($("avatarLevel") && speakingLocal) {
          $("avatarLevel").style.width = Math.min(100, Math.max(20, Math.random() * 70 + 20)) + "%";
        }
      }, PROGRESS_MS);
    }, Math.max(0, (t - now) * 1000));
  }
}
function onEnd(m) {
  const t = cur;
  if (!t || t.id !== m.turn_id || t.dead) return;
  t.finTimer = setTimeout(() => {
    if (t.dead) return;
    stopTimers(t);
    send({ type: "playback", turn_id: t.id, event: "finished", played_ms: t.totalMs });
    t.dead = true; speakingLocal = false; setBadge();
    if ($("avatarVolWrap")) $("avatarVolWrap").hidden = true;
    if ($("avatarLevel")) $("avatarLevel").style.width = "0";
  }, Math.max(0, (t.cursor - ctx.currentTime) * 1000) + 10);
}
function killTurn(t, fadeMs, report) {
  const played = playedMs(t);
  t.dead = true; deadTurns.add(t.id); stopTimers(t);
  const now = ctx.currentTime;
  t.gain.gain.cancelScheduledValues(now);
  t.gain.gain.setValueAtTime(t.gain.gain.value, now);
  t.gain.gain.linearRampToValueAtTime(0, now + fadeMs / 1000);
  setTimeout(() => { t.sources.forEach((s) => { try { s.stop(); } catch (e) {} }); t.gain.disconnect(); }, fadeMs + 20);
  if (report) send({ type: "playback", turn_id: t.id, event: "stopped", played_ms: played });
  if ($("avatarVolWrap")) $("avatarVolWrap").hidden = true;
  if ($("avatarLevel")) $("avatarLevel").style.width = "0";
}
function onInterrupt(m) {
  deadTurns.add(m.turn_id);
  if (turnEls[m.turn_id]) turnEls[m.turn_id].classList.add("cut");
  if (cur && cur.id === m.turn_id && !cur.dead) {
    killTurn(cur, m.fade_ms || 40, true);
    speakingLocal = false; setBadge();
  }
}

// ---------- 서버 메시지 ----------
function onHello(m) {
  if (m.session_state) currentSessionState = m.session_state;
  if (m.mode && !localStorage.getItem("healthup_mode")) {
    setMode(m.mode, false);
  }
  $("engines").textContent = `llm=${m.engines.llm} tts=${m.engines.tts} stt=${m.engines.stt}` +
    (m.first_min != null ? ` first_min=${m.first_min}` : "") +
    (m.vad_silence_ms != null ? ` vad_silence=${m.vad_silence_ms}ms` : "");
  const voices = m.voices || [], sel = $("voice");
  sel.textContent = "";
  voices.forEach((v) => { const o = document.createElement("option"); o.value = v.id; o.textContent = v.label || v.id; sel.appendChild(o); });
  if (m.voice) sel.value = m.voice;
  const opts = m.stt_options || [], ss = $("stt");
  ss.textContent = "";
  opts.forEach((o) => { const e = document.createElement("option"); e.value = o.id; e.disabled = !o.available;
    e.textContent = (o.label || o.id) + (o.available ? "" : " (없음)"); ss.appendChild(e); });
  if (m.stt) ss.value = m.stt;
  const eo = m.endpoint_options || [], es = $("endpoint");
  es.textContent = "";
  eo.forEach((o) => { const e = document.createElement("option"); e.value = o.id; e.textContent = o.label || o.id; es.appendChild(e); });
  if (m.endpoint) es.value = m.endpoint;
  $("epCard").hidden = true; epBadge = null; ep = null;
  if (m.backchannel != null) $("backchannel").checked = m.backchannel;
  applyVisibility();
  updateAvatarHint();
}

function onMessage(ev) {
  if (typeof ev.data !== "string") { if (ctx) onFrame(ev.data); return; }
  const m = JSON.parse(ev.data);
  switch (m.type) {
    case "hello": onHello(m); break;
    case "mode_ready":
      if (m.mode === "demo" || m.mode === "dev") {
        setMode(m.mode, false);
      }
      break;
    case "config":
      for (const k of ["wake", "face", "show_ai_notice", "show_camera_badge", "show_conn_status", "show_dev_info"]) {
        if (k in m) {
          prefs[k] = !!m[k];
        }
      }
      if (m.consent) {
        faceConsented = true;
        sessionStorage.setItem("healthup_face_consent", "true");
        if ($("faceConsentModal")) $("faceConsentModal").hidden = true;
      }
      if (m.face != null) {
        if (m.face) {
          if (!faceConsented) {
            prefs.face = false;
            if ($("prefFace")) $("prefFace").checked = false;
            if ($("faceConsentModal")) $("faceConsentModal").hidden = false;
            send({ type: "config", face: false });
          } else {
            if ($("prefFace")) $("prefFace").checked = true;
            startCamera();
          }
        } else {
          stopCamera();
          if ($("faceConsentModal")) $("faceConsentModal").hidden = true;
        }
      }
      savePrefs();
      applyVisibility();
      break;
    case "duck": if (ctx && master) master.gain.setTargetAtTime(m.on ? DUCK_GAIN : 1, ctx.currentTime, 0.03); break;
    case "dev_event":
      if (m.event === "duck") {
        const lat = m.duck_latency_ms != null ? ` (지연 ${m.duck_latency_ms}ms)` : "";
        addMsg("dev", `[barge-in] 덕킹 ${m.on ? "켜짐" : "꺼짐"}${lat}`);
      } else if (m.event === "barge_in") {
        addMsg("dev", `[barge-in] 판별: ${m.result}` + (m.text ? ` ("${m.text}")` : "") + (m.dur_ms ? ` (${m.dur_ms}ms)` : ""));
      } else if (m.event === "interrupt") {
        addMsg("dev", `[barge-in] 끊김: #${m.turn_id} (재생 ${m.played_ms}ms)`);
      } else if (m.event === "intent") {
        addMsg("dev", `[의도] ${m.intent} (등급: ${m.grade}, 규칙: ${m.rule}, ${m.classify_ms}ms)`);
      } else if (m.event === "hint") {
        const emoStr = m.emotion ? ` (음성: ${m.emotion})` : "";
        const faceStr = m.face ? ` (표정: ${m.face})` : "";
        addMsg("dev", `[감정/hint] 규칙: ${m.rule}${emoStr}${faceStr}`);
      }
      break;
    case "session_state":
      addMsg("dev", `[세션] 상태: ${m.state}`);
      currentSessionState = m.state;
      updateAvatarHint();
      break;
    case "backchannel_ignored": addMsg("alt", `맞장구로 보고 무시: ${m.text || "(소리)"}`); break;
    case "pong": if (typeof m.t === "number") $("rtt").textContent = `RTT ${Math.round(performance.now() - m.t)} ms`; break;
    case "stt_ready": $("stt").value = m.stt; $("err").textContent = ""; break;
    case "retranscript": addMsg("alt", `다시 인식 [${m.stt} ${fmt(m.stt_ms)}ms] ${m.text || "(빈 결과)"}`); break;
    case "turn_eval": onTurnEval(m); break;
    case "endpoint_ready": $("endpoint").value = m.endpoint; epBadge = null; ep = null; $("epCard").hidden = true; setBadge(); break;
    case "avatar_state": epBadge = null; lastState = m.state; setBadge(); break;
    case "vad":
      $("vad").classList.toggle("on", m.speaking);
      speakingLocal = !!m.speaking;
      if (m.speaking) {
        faceFrames = [];
        totalSpeechFrames = 0;
      } else {
        if (prefs.face && !faceModelFailed && faceFrames.length > 0) {
          const counts = {};
          const confSums = {};
          for (const f of faceFrames) {
            const lbl = f.label || "NEUTRAL";
            counts[lbl] = (counts[lbl] || 0) + 1;
            confSums[lbl] = (confSums[lbl] || 0) + (f.conf || 0.8);
          }
          let dominant = "NEUTRAL";
          let maxCount = 0;
          for (const k in counts) {
            if (counts[k] > maxCount) {
              maxCount = counts[k];
              dominant = k;
            }
          }
          const avgConf = confSums[dominant] / maxCount;
          const totalFrames = Math.max(totalSpeechFrames, faceFrames.length, 1);
          const coverage = Math.min(1.0, maxCount / totalFrames);
          const faceSummary = {
            type: "face_summary",
            label: dominant,
            conf: Math.round(avgConf * 100) / 100,
            coverage: Math.round(coverage * 100) / 100,
            n_frames: faceFrames.length,
            total_frames: totalFrames,
          };
          rememberFaceSummary(faceSummary);
          send(faceSummary);
        }
        faceFrames = [];
        totalSpeechFrames = 0;
      }
      break;
    case "transcript": addMsg("user", (m.utt_id != null ? `#${m.utt_id} ` : "") + m.text + (m.emotion || m.event ? `  [${[m.emotion, m.event].filter(Boolean).join(", ")}]` : "") +
      (m.stt ? `  [${m.stt} ${fmt(m.stt_ms)}ms]` : "") +
      (m.tone ? `  [톤 ${m.tone}]` : "")); break;
    case "assistant_delta":
      if (!turnEls[m.turn_id]) turnEls[m.turn_id] = addMsg("bot", "");
      turnEls[m.turn_id].textContent += m.text; $("log").scrollTop = $("log").scrollHeight; break;
    case "audio_begin": if (ctx) onBegin(m); break;
    case "audio_end": if (ctx) onEnd(m); break;
    case "interrupt": if (ctx) onInterrupt(m); break;
    case "turn_done":
      if (turnEls[m.turn_id]) {
        if (m.interrupted) turnEls[m.turn_id].classList.add("cut");
        if (m.spoken_text != null) {
          let sp = turnEls[m.turn_id].querySelector(".spoken-text");
          if (!sp) {
            sp = document.createElement("div");
            sp.className = "spoken-text";
            turnEls[m.turn_id].appendChild(sp);
          }
          sp.textContent = `[들린 부분: "${m.spoken_text}"]`;
        }
      }
      setLatency(m.turn_id, m.trace); break;
    case "trace_update": setLatency(m.turn_id, m.trace); break;
    case "reset_done":
      $("log").innerHTML = "";
      $("lat").innerHTML = "";
      for (const k in traces) delete traces[k];
      for (const k in latRows) delete latRows[k];
      for (const k in turnEls) delete turnEls[k];
      updateLatSummary();
      currentSessionState = "idle";
      updateAvatarHint();
      break;
    case "error": $("err").textContent = m.message; break;
  }
}

// ---------- 시작 ----------
function wsUrl() {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws" +
    (TOKEN ? "?t=" + encodeURIComponent(TOKEN) : "");
}

function connect() {
  const mine = ws = window.ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";
  ws.onmessage = onMessage;
  ws.onopen = () => {
    if (ws !== mine) return;
    retries = 0; setRunning(true);
    send({
      type: "config",
      mode: currentMode,
      wake: prefs.wake,
      face: prefs.face,
      show_dev_info: prefs.show_dev_info,
      barge_in: $("bargein").checked,
      backchannel: $("backchannel").checked
    });
    if (!prefs.wake && currentSessionState === "idle") {
      send({ type: "session_start" });
    }
    lastState = "listening"; epBadge = null; setBadge();
  };
  ws.onclose = () => {
    if (ws !== mine) return;
    resetPlayback();
    if (!wantRun) {
      setRunning(false);
      $("badge").textContent = "연결 끊김";
      $("badge").className = "badge";
      if ($("avatarStatusText")) {
        $("avatarStatusText").textContent = "연결 끊김";
        $("avatarStatusText").className = "avatar-status";
      }
      return;
    }
    $("badge").textContent = "재연결 중";
    $("badge").className = "badge";
    if ($("avatarStatusText")) {
      $("avatarStatusText").textContent = "재연결 중";
      $("avatarStatusText").className = "avatar-status";
    }
    $("rtt").textContent = "RTT - ms";
    const delay = Math.min(5000, 500 * 2 ** retries++);
    retryTimer = setTimeout(() => { retryTimer = null; if (wantRun) connect(); }, delay);
  };
}

window.sendPcm = async function(loudFrames, quietFrames) {
  for (let i = 0; i < loudFrames; i++) {
    const chunk = new Int16Array(320).fill(8000);
    if (ws && ws.readyState === 1) ws.send(chunk.buffer);
    await new Promise(r => setTimeout(r, 20));
  }
  for (let i = 0; i < quietFrames; i++) {
    const chunk = new Int16Array(320).fill(0);
    if (ws && ws.readyState === 1) ws.send(chunk.buffer);
    await new Promise(r => setTimeout(r, 20));
  }
};

function resetPlayback() {
  if (cur && !cur.dead) { killTurn(cur, 0, false); }
  cur = null; deadTurns.clear(); speakingLocal = false;
}

async function start() {
  $("start").disabled = true;
  if (!ctx || ctx.state === "closed") {
    ctx = new AudioContext({ latencyHint: "interactive" });
    master = ctx.createGain(); master.connect(ctx.destination);
  }
  await ctx.resume();
  wantRun = true; retries = 0;
  connect();
  setRunning(true);
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    if (!workletLoaded) { await ctx.audioWorklet.addModule("/static/mic-worklet.js"); workletLoaded = true; }
    const node = micNode = new AudioWorkletNode(ctx, "mic-processor");
    const mute = ctx.createGain(); mute.gain.value = 0;
    ctx.createMediaStreamSource(micStream).connect(node); node.connect(mute); mute.connect(ctx.destination);
    node.port.onmessage = (e) => {
      const pcm = new Int16Array(e.data);
      let s = 0; for (let i = 0; i < pcm.length; i++) s += pcm[i] * pcm[i];
      const db = 20 * Math.log10(Math.sqrt(s / pcm.length) / 32768 + 1e-6);
      $("level").style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + "%";
      if (!window.micMuted && ws && ws.readyState === 1 && ws.bufferedAmount < 64000) ws.send(e.data);
    };
  } catch (e) {
    $("err").textContent = "마이크를 쓸 수 없습니다(" + e.message + "). 텍스트 입력으로 시험하세요. localhost 또는 HTTPS 주소여야 합니다.";
  }
}

setInterval(() => send({ type: "ping", t: performance.now() }), 2000);

function updateAvatarHint() {
  if (!$("avatarCard") || !$("avatarHint")) return;
  $("avatarCard").classList.toggle("running", running);
  if (!running) {
    $("avatarHint").textContent = "화면을 터치하여 대화를 시작하세요";
  } else if (currentSessionState === "idle") {
    if (prefs.wake) {
      $("avatarHint").textContent = "마이크 대기 중 ('시작하자'라고 말씀해 주세요)";
    } else {
      $("avatarHint").textContent = "대화가 종료되었습니다 (화면을 터치하여 대화를 시작하세요)";
    }
  } else {
    $("avatarHint").textContent = "대화 진행 중입니다 (편하게 말씀하세요)";
  }
}

function setRunning(on) {
  running = on;
  $("start").textContent = on ? "대화 중지" : "시작";
  $("start").classList.toggle("primary", !on);
  $("start").disabled = false;
  $("stopAnswer").disabled = !on;
  updateAvatarHint();
}

function stopAll() {
  wantRun = false; clearTimeout(retryTimer); retryTimer = null;
  send({ type: "stop" });
  stopCamera();
  if (cur && !cur.dead) { killTurn(cur, 40, false); speakingLocal = false; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (micNode) { micNode.port.onmessage = null; micNode.disconnect(); micNode = null; }
  $("level").style.width = "0"; $("vad").classList.remove("on");
  const old = ws; ws = null;
  if (old) old.onmessage = null;
  setTimeout(() => { try { old.close(); } catch (e) {} }, 200);
  lastState = "listening"; epBadge = null; ep = null; setRunning(false);
  $("badge").textContent = "중지됨"; $("badge").className = "badge"; $("rtt").textContent = "RTT - ms";
  if ($("avatarStatusText")) {
    $("avatarStatusText").textContent = "중지됨";
    $("avatarStatusText").className = "avatar-status";
  }
}

// ---------- 얼굴 표정 카메라 및 모델 제어 (F2) ----------
function faceEngineLabel() {
  if (!window.faceapi) return "없음";
  if (faceModelFailed) return "face-api, 모델 없음";
  if (faceLandmarkMissing) return "face-api, 랜드마크 없음";
  return "face-api";
}

function renderFaceHud(patch) {
  if (patch) Object.assign(faceHudLive, patch);
  if (currentMode !== "dev" || !$("faceHud") || !window.FaceHud) return;
  const view = window.FaceHud.lines({
    detected: faceHudLive.detected,
    score: faceHudLive.score,
    faceCount: faceHudLive.faceCount,
    emotions: faceHudLive.emotions,
    fps: faceHudLive.fps,
    lastSent: lastFaceSent,
    lastSentAt: lastFaceSentAt,
    engine: faceEngineLabel(),
    camera: faceCameraState,
    cameraError: faceCameraError,
  });
  const fields = {
    faceHudDetected: view.detected,
    faceHudScore: view.score,
    faceHudCount: view.count,
    faceHudEmotions: view.emotions,
    faceHudFps: view.fps,
    faceHudLast: view.lastSent,
    faceHudEngine: view.engine,
    faceHudCamera: view.camera,
  };
  for (const id in fields) {
    const el = $(id);
    if (el) el.textContent = fields[id];
  }
}

function rememberFaceSummary(summary) {
  lastFaceSent = {
    label: summary.label,
    conf: summary.conf,
    coverage: summary.coverage,
    n_frames: summary.n_frames,
    total_frames: summary.total_frames,
  };
  lastFaceSentAt = new Date().toLocaleTimeString("ko-KR", { hour12: false });
  renderFaceHud();
}

function noteFaceFps() {
  const now = performance.now();
  faceFpsMarks.push(now);
  while (faceFpsMarks.length && now - faceFpsMarks[0] > 1000) faceFpsMarks.shift();
  faceHudLive.fps = faceFpsMarks.length;
}

function clearFaceOverlay() {
  const canvas = $("faceOverlay");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function drawFaceOverlay(video, faces) {
  const canvas = $("faceOverlay");
  if (!canvas || !video) return;
  const rect = canvas.getBoundingClientRect();
  const cw = Math.max(1, Math.round(rect.width));
  const ch = Math.max(1, Math.round(rect.height));
  if (canvas.width !== cw) canvas.width = cw;
  if (canvas.height !== ch) canvas.height = ch;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, cw, ch);
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh || !faces || !faces.length) return;
  const scale = Math.max(cw / vw, ch / vh);
  const ox = (cw - vw * scale) / 2;
  const oy = (ch - vh * scale) / 2;
  for (const face of faces) {
    const box = face.detection && face.detection.box;
    if (box) {
      ctx.strokeStyle = "#2f6fed";
      ctx.lineWidth = 2;
      ctx.strokeRect(ox + box.x * scale, oy + box.y * scale, box.width * scale, box.height * scale);
    }
    const pts = face.landmarks && face.landmarks.positions;
    if (pts && pts.length) {
      ctx.fillStyle = "#46c27d";
      for (const p of pts) {
        ctx.fillRect(ox + p.x * scale - 1, oy + p.y * scale - 1, 2, 2);
      }
    }
  }
}

function emotionRows(expressions) {
  if (!expressions || typeof expressions.asSortedArray !== "function") return [];
  return expressions.asSortedArray().slice(0, 3).map((row) => ({
    label: String(row.expression || "").toUpperCase(),
    score: row.probability,
  }));
}

function pushFaceFrame(expressions) {
  const rows = emotionRows(expressions);
  if (!rows.length) return;
  faceFrames.push({
    label: rows[0].label,
    conf: Math.round(rows[0].score * 100) / 100,
  });
}

async function loadFaceModelIfNeeded() {
  if (!window.faceapi || faceModelLoading) return;
  const nets = window.faceapi.nets;
  const needDetector = !nets.tinyFaceDetector.params;
  const needExpr = !nets.faceExpressionNet.params;
  const needLandmark = !faceLandmarksReady && !faceLandmarkMissing && nets.faceLandmark68TinyNet && !nets.faceLandmark68TinyNet.params;
  if (!needDetector && !needExpr && !needLandmark) return;
  faceModelLoading = true;
  try {
    if (needDetector) await nets.tinyFaceDetector.loadFromUri("/static/models/face-api");
    if (needExpr) await nets.faceExpressionNet.loadFromUri("/static/models/face-api");
    faceModelFailed = false;
    addMsg("dev", "[표정 모델] face-api 로컬 모델 로드 완료");
    if (needLandmark) {
      try {
        await nets.faceLandmark68TinyNet.loadFromUri("/static/models/face-api");
        faceLandmarksReady = !!nets.faceLandmark68TinyNet.params;
        faceLandmarkMissing = !faceLandmarksReady;
      } catch (e) {
        faceLandmarksReady = false;
        faceLandmarkMissing = true;
        console.warn("랜드마크 모델 로드 실패:", e);
      }
    }
  } catch (e) {
    faceModelFailed = true;
    faceCameraError = (e && e.message) || String(e);
    console.warn("face-api 모델 로드 실패:", e);
    addMsg("dev", `[표정 모델] 모델 로드 실패: ${e.message || e} (표정 분석 비활성화)`);
  } finally {
    faceModelLoading = false;
    renderFaceHud();
  }
}

async function startCamera() {
  if (faceStream) return;
  try {
    const video = $("faceVideo");
    faceStream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 } }
    });
    faceCameraState = "허용";
    faceCameraError = "";
    if (video) {
      video.srcObject = faceStream;
      video.play().catch(() => {});
    }
    renderFaceHud();
    loadFaceModelIfNeeded();
    if (faceIntervalId) clearInterval(faceIntervalId);
    faceIntervalId = setInterval(analyzeFaceFrame, 100);
  } catch (err) {
    const name = err && err.name;
    faceCameraState = (name === "NotAllowedError" || name === "PermissionDeniedError") ? "거부" : "오류";
    faceCameraError = (err && (err.message || err.name)) || "카메라 오류";
    console.warn("카메라 활성화 실패:", err);
    renderFaceHud();
  }
}

function stopCamera() {
  if (faceIntervalId) {
    clearInterval(faceIntervalId);
    faceIntervalId = null;
  }
  if (faceStream) {
    faceStream.getTracks().forEach((t) => t.stop());
    faceStream = null;
  }
  const video = $("faceVideo");
  if (video) {
    video.srcObject = null;
  }
  faceFrames = [];
  totalSpeechFrames = 0;
  faceCameraState = "대기";
  faceCameraError = "";
  faceHudLive = { detected: false, score: null, faceCount: 0, emotions: [], fps: null };
  clearFaceOverlay();
  renderFaceHud();
}

async function analyzeFaceFrame() {
  if (faceAnalyzing) return;
  if (faceModelFailed) {
    renderFaceHud({ detected: false, score: null, faceCount: 0, emotions: [] });
    return;
  }
  faceAnalyzing = true;
  try {
    if (speakingLocal) totalSpeechFrames++;
    if (window._mockFaceEmotion && speakingLocal) {
      const conf = window._mockFaceConf || 0.85;
      faceFrames.push({ label: window._mockFaceEmotion, conf: conf });
      noteFaceFps();
      renderFaceHud({
        detected: true,
        score: conf,
        faceCount: 1,
        emotions: [{ label: String(window._mockFaceEmotion).toUpperCase(), score: conf }],
      });
      return;
    }
    const video = $("faceVideo");
    if (!video || video.readyState < 2) return;
    const showHud = currentMode === "dev";
    if (!speakingLocal && !showHud) return;
    if (!window.faceapi || !window.faceapi.nets.tinyFaceDetector.params) return;
    const opts = new window.faceapi.TinyFaceDetectorOptions();
    if (!showHud) {
      const detection = await window.faceapi.detectSingleFace(video, opts).withFaceExpressions();
      if (speakingLocal && detection && detection.expressions) pushFaceFrame(detection.expressions);
      return;
    }
    let task = window.faceapi.detectAllFaces(video, opts);
    if (faceLandmarksReady) task = task.withFaceLandmarks(true);
    const faces = await task.withFaceExpressions();
    noteFaceFps();
    drawFaceOverlay(video, faces);
    const best = (faces || []).slice().sort((a, b) => {
      const as = (a.detection && a.detection.score) || 0;
      const bs = (b.detection && b.detection.score) || 0;
      return bs - as;
    })[0];
    if (speakingLocal && best && best.expressions) pushFaceFrame(best.expressions);
    if (faceCameraState === "허용") faceCameraError = "";
    renderFaceHud({
      detected: !!(faces && faces.length),
      score: best && best.detection ? best.detection.score : null,
      faceCount: faces ? faces.length : 0,
      emotions: best ? emotionRows(best.expressions) : [],
    });
  } catch (e) {
    faceCameraError = (e && e.message) || String(e);
    renderFaceHud({ detected: false, score: null, faceCount: 0, emotions: [] });
  } finally {
    faceAnalyzing = false;
  }
}

function setFaceEnabled(enabled) {
  prefs.face = enabled;
  savePrefs();
  send({ type: "config", face: prefs.face });
  applyVisibility();
  if (enabled) {
    startCamera();
  } else {
    stopCamera();
  }
}

window.injectFaceSummary = function(summary) {
  const payload = {
    type: "face_summary",
    label: summary.label || "HAPPY",
    conf: summary.conf != null ? summary.conf : 0.9,
    coverage: summary.coverage != null ? summary.coverage : 0.8,
    n_frames: summary.n_frames != null ? summary.n_frames : 10,
    total_frames: summary.total_frames != null ? summary.total_frames : 10,
  };
  rememberFaceSummary(payload);
  send(payload);
};
window.setMockFaceEmotion = function(label, conf) {
  window._mockFaceEmotion = label;
  window._mockFaceConf = conf;
};

// ---------- 이벤트 리스너 등록 ----------
$("start").onclick = () => (running ? stopAll() : start());
$("stopAnswer").onclick = () => send({ type: "stop" });
$("endpoint").onchange = () => send({ type: "config", endpoint: $("endpoint").value });
$("stt").onchange = () => send({ type: "config", stt: $("stt").value });
$("retry").onclick = () => send({ type: "retranscribe" });
$("voice").onchange = () => send({ type: "config", voice: $("voice").value });
$("backchannel").onchange = () => send({ type: "config", backchannel: $("backchannel").checked });
$("bargein").onchange = () => send({ type: "config", barge_in: $("bargein").checked });
$("reset").onclick = () => send({ type: "reset" });
$("form").onsubmit = (e) => {
  e.preventDefault();
  const t = $("text").value.trim(); if (!t) return;
  $("text").value = "";
  send({ type: "text_input", text: t });
};

// 자리표시 카드 클릭 시 최초 시작 또는 재시작 터치
$("avatarCard").onclick = () => {
  if (!running) {
    start();
  } else if (currentSessionState === "idle") {
    send({ type: "session_start" });
  }
};

// 설정 패널 및 모드 토글
$("settingsBtn").onclick = () => {
  $("settingsPanel").hidden = !$("settingsPanel").hidden;
};

$("modeDemo").onchange = () => setMode("demo");
$("modeDev").onchange = () => setMode("dev");

// 개별 기능 체크박스
$("prefWake").onchange = (e) => {
  prefs.wake = e.target.checked;
  savePrefs();
  send({ type: "config", wake: prefs.wake });
  updateAvatarHint();
};

$("prefFace").onchange = (e) => {
  if (e.target.checked) {
    if (!faceConsented) {
      $("prefFace").checked = false;
      if ($("faceConsentModal")) $("faceConsentModal").hidden = false;
    } else {
      setFaceEnabled(true);
    }
  } else {
    setFaceEnabled(false);
  }
};

if ($("btnFaceConsentAgree")) {
  $("btnFaceConsentAgree").onclick = () => {
    faceConsented = true;
    sessionStorage.setItem("healthup_face_consent", "true");
    if ($("faceConsentModal")) $("faceConsentModal").hidden = true;
    $("prefFace").checked = true;
    setFaceEnabled(true);
  };
}

if ($("btnFaceConsentReject")) {
  $("btnFaceConsentReject").onclick = () => {
    if ($("faceConsentModal")) $("faceConsentModal").hidden = true;
    $("prefFace").checked = false;
    setFaceEnabled(false);
  };
}

if ($("faceConsentModal")) {
  $("faceConsentModal").addEventListener("click", (e) => {
    if (e.target === $("faceConsentModal")) {
      if ($("btnFaceConsentReject")) $("btnFaceConsentReject").click();
    }
  });
}

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("faceConsentModal") && !$("faceConsentModal").hidden) {
    if ($("btnFaceConsentReject")) $("btnFaceConsentReject").click();
  }
});

$("prefAiNotice").onchange = (e) => {
  if (!e.target.checked) {
    const ok = window.confirm("AI 기본법 제31조에 따른 법적 의무 고지입니다. 내부 시험 목적으로만 숨기시겠습니까?");
    if (!ok) {
      e.target.checked = true;
      return;
    }
  }
  prefs.show_ai_notice = e.target.checked;
  savePrefs();
  send({ type: "config", show_ai_notice: prefs.show_ai_notice });
};

$("prefCameraBadge").onchange = (e) => {
  prefs.show_camera_badge = e.target.checked;
  savePrefs();
  send({ type: "config", show_camera_badge: prefs.show_camera_badge });
};

$("prefConnStatus").onchange = (e) => {
  prefs.show_conn_status = e.target.checked;
  savePrefs();
  send({ type: "config", show_conn_status: prefs.show_conn_status });
};

$("prefDevInfo").onchange = (e) => {
  prefs.show_dev_info = e.target.checked;
  savePrefs();
  send({ type: "config", show_dev_info: prefs.show_dev_info });
};

function applyStoredFace() {
  const hud = window.FaceHud;
  const plan = hud && hud.restoreFaceOnLoad
    ? hud.restoreFaceOnLoad(!!prefs.face, !!faceConsented, currentMode)
    : { face: !!prefs.face && !!faceConsented, startCamera: !!prefs.face && !!faceConsented, showPreview: false };
  prefs.face = !!plan.face;
  if ($("prefFace")) $("prefFace").checked = !!plan.face;
  if (plan.startCamera) startCamera();
}

// 초기화 적용. 동의와 저장값이 맞으면 카메라를 켠다. 동의가 없으면 체크를 끈다.
setMode(currentMode, false);
applyStoredFace();
