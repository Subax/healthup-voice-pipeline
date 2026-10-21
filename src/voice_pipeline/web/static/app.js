"use strict";
const $ = (id) => document.getElementById(id);
const JITTER = 0.06;        // 초기 지터 버퍼 (초)
const PROGRESS_MS = 100;    // 재생 위치 보고 주기

let ws = null, ctx = null, master = null;
let workletLoaded = false;
let micStream = null, micNode = null, running = false;
let cur = null;             // 현재 재생 중인 턴
const deadTurns = new Set();
let wantRun = false;        // 「시작」을 눌러 실행 중 (끊기면 재연결한다)
let retries = 0, retryTimer = null;
const TOKEN = new URLSearchParams(location.search).get("t");  // 인증 토큰 (?t=)
let speakingLocal = false, lastState = "listening";
const turnEls = {};         // turn_id -> 말풍선
const latRows = {};

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

// ---------- UI ----------
// 턴 종료 판단 배지 (smart 모드). 확정 뒤 첫 avatar_state가 오면 풀린다
let epBadge = null;
function setBadge() {
  if (epBadge) { $("badge").textContent = epBadge.text; $("badge").className = "badge " + epBadge.cls; return; }
  const st = speakingLocal && lastState !== "safety" ? "speaking" : lastState;
  const names = { listening: "듣는 중", thinking: "생각 중", speaking: "말하는 중", safety: "안전 안내" };
  $("badge").textContent = names[st] || st;
  $("badge").className = "badge " + st;
}
function addMsg(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.textContent = text;
  $("log").appendChild(d); $("log").scrollTop = $("log").scrollHeight;
  return d;
}
function fmt(v) { return v == null ? "-" : Math.round(v); }
const traces = {};
function setLatency(turn_id, upd) {
  const tr = traces[turn_id] = traces[turn_id] || {};
  for (const k in upd) if (upd[k] != null) tr[k] = upd[k]; // 늦게 오는 값(playback_start)만 채운다
  let tr_el = latRows[turn_id];
  if (!tr_el) { tr_el = latRows[turn_id] = document.createElement("tr"); $("lat").prepend(tr_el); }
  const cells = [`#${turn_id}`, tr.stt_ms, tr.turn_confirmed, tr.llm_first_token, tr.first_chunk, tr.backchannel, tr.tts_first_audio, tr.playback_start];
  tr_el.innerHTML = cells.map((c, i) => `<td>${i ? fmt(c) : c}</td>`).join("");
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
  if (m.stage === "reopened") {  // 확정했던 턴의 말풍선에 취소선을 긋는다. 이어 붙인 전사는 새 말풍선으로 온다
    const us = document.querySelectorAll("#log .msg.user");
    if (us.length) us[us.length - 1].classList.add("merged");
  }
  const p = m.smart_turn_p == null ? "-" : m.smart_turn_p.toFixed(2);
  const e = m.ending || {};
  $("epCard").hidden = false;
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
  if (cur && !cur.dead) killTurn(cur, 0, false);
  const gain = ctx.createGain(); gain.connect(master);
  cur = { id: m.turn_id, rate: m.sample_rate, gain, cursor: 0, chunks: [], ptr: 0, doneMs: 0, totalMs: 0,
          sources: [], started: false, dead: false, prog: null, startTimer: null, finTimer: null };
  speakingLocal = true; setBadge();
}
function onFrame(buf) {
  const dv = new DataView(buf);
  const tid = dv.getUint32(0, true);
  if (!cur || cur.dead || cur.id !== tid || deadTurns.has(tid)) return; // 지난 턴 프레임은 버린다
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
  else if (t < now + 0.005) t = now + 0.02;   // 언더런이면 다시 맞춘다
  src.start(t);
  const d = n / cur.rate;
  cur.cursor = t + d; cur.totalMs += d * 1000;
  cur.chunks.push({ t, d }); cur.sources.push(src);
  if (cur.chunks.length === 1) { // 첫 프레임이 실제로 재생되는 시각에 started
    const turn = cur;
    turn.startTimer = setTimeout(() => {
      if (turn.dead) return;
      turn.started = true;
      send({ type: "playback", turn_id: turn.id, event: "started", played_ms: 0 });
      turn.prog = setInterval(() => send({ type: "playback", turn_id: turn.id, event: "progress", played_ms: playedMs(turn) }), PROGRESS_MS);
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
  $("engines").textContent = `llm=${m.engines.llm} tts=${m.engines.tts} stt=${m.engines.stt}` +
    (m.first_min != null ? ` first_min=${m.first_min}` : "") +
    (m.vad_silence_ms != null ? ` vad_silence=${m.vad_silence_ms}ms` : "");
  const voices = m.voices || [], sel = $("voice");
  sel.textContent = "";
  voices.forEach((v) => { const o = document.createElement("option"); o.value = v.id; o.textContent = v.label || v.id; sel.appendChild(o); });
  if (m.voice) sel.value = m.voice;
  $("voiceWrap").hidden = voices.length === 0;
  const opts = m.stt_options || [], ss = $("stt");
  ss.textContent = "";
  opts.forEach((o) => { const e = document.createElement("option"); e.value = o.id; e.disabled = !o.available;
    e.textContent = (o.label || o.id) + (o.available ? "" : " (없음)"); ss.appendChild(e); });
  if (m.stt) ss.value = m.stt;
  $("sttWrap").hidden = opts.length === 0;
  const eo = m.endpoint_options || [], es = $("endpoint");
  es.textContent = "";
  eo.forEach((o) => { const e = document.createElement("option"); e.value = o.id; e.textContent = o.label || o.id; es.appendChild(e); });
  if (m.endpoint) es.value = m.endpoint;
  $("epWrap").hidden = eo.length === 0;
  $("epCard").hidden = true; epBadge = null; ep = null;
  if (m.backchannel != null) $("backchannel").checked = m.backchannel;
}
function onMessage(ev) {
  if (typeof ev.data !== "string") { if (ctx) onFrame(ev.data); return; }
  const m = JSON.parse(ev.data);
  switch (m.type) {
    case "hello": onHello(m); break;
    case "pong": if (typeof m.t === "number") $("rtt").textContent = `RTT ${Math.round(performance.now() - m.t)} ms`; break;
    case "stt_ready": $("stt").value = m.stt; $("err").textContent = ""; break;
    case "retranscript": addMsg("alt", `다시 인식 [${m.stt} ${fmt(m.stt_ms)}ms] ${m.text || "(빈 결과)"}`); break;
    case "turn_eval": onTurnEval(m); break;
    case "endpoint_ready": $("endpoint").value = m.endpoint; epBadge = null; ep = null; $("epCard").hidden = true; setBadge(); break;
    case "avatar_state": epBadge = null; lastState = m.state; setBadge(); break;
    case "vad": $("vad").classList.toggle("on", m.speaking); break;
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
      if (m.interrupted && turnEls[m.turn_id]) turnEls[m.turn_id].classList.add("cut");
      setLatency(m.turn_id, m.trace); break;
    case "trace_update": setLatency(m.turn_id, m.trace); break;
    case "reset_done": $("log").innerHTML = ""; $("lat").innerHTML = ""; break;
    case "error": $("err").textContent = m.message; break;
  }
}

// ---------- 시작 ----------
function wsUrl() {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws" +
    (TOKEN ? "?t=" + encodeURIComponent(TOKEN) : "");
}
// ws를 연다. 열리면 config를 보낸다. 끊기면(wantRun일 때) 지수 백오프로 다시 연다
function connect() {
  const mine = ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";
  ws.onmessage = onMessage;
  ws.onopen = () => {
    if (ws !== mine) return;
    retries = 0; setRunning(true);
    send({ type: "config", barge_in: $("bargein").checked, backchannel: $("backchannel").checked });
    lastState = "listening"; epBadge = null; setBadge();
  };
  ws.onclose = () => {
    if (ws !== mine) return;
    resetPlayback();  // 서버 세션이 새로 생기므로 재생 중이던 턴은 버린다
    if (!wantRun) { setRunning(false); $("badge").textContent = "연결 끊김"; $("badge").className = "badge"; return; }
    $("badge").textContent = "재연결 중"; $("badge").className = "badge";
    $("rtt").textContent = "RTT - ms";
    const delay = Math.min(5000, 500 * 2 ** retries++);
    retryTimer = setTimeout(() => { retryTimer = null; if (wantRun) connect(); }, delay);
  };
}
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
      if (ws && ws.readyState === 1 && ws.bufferedAmount < 64000) ws.send(e.data);
    };
  } catch (e) {
    $("err").textContent = "마이크를 쓸 수 없습니다(" + e.message + "). 텍스트 입력으로 시험하세요. localhost 또는 HTTPS 주소여야 합니다.";
  }
}
// RTT: 2초마다 ping을 보내고 pong이 오면 표시한다
setInterval(() => send({ type: "ping", t: performance.now() }), 2000);

function setRunning(on) {
  running = on;
  $("start").textContent = on ? "대화 중지" : "시작";
  $("start").classList.toggle("primary", !on);
  $("start").disabled = false;
  $("stopAnswer").disabled = !on;
}
function stopAll() {
  wantRun = false; clearTimeout(retryTimer); retryTimer = null;
  send({ type: "stop" });
  if (cur && !cur.dead) { killTurn(cur, 40, false); speakingLocal = false; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (micNode) { micNode.port.onmessage = null; micNode.disconnect(); micNode = null; }
  $("level").style.width = "0"; $("vad").classList.remove("on");
  const old = ws; ws = null;   // onclose가 상태 표시를 덮어쓰지 않게 먼저 뗀다
  if (old) old.onmessage = null; // 중지 뒤 늦게 오는 avatar_state가 「중지됨」을 덮지 않게 한다
  setTimeout(() => { try { old.close(); } catch (e) {} }, 200); // stop 메시지가 나간 뒤 닫는다
  lastState = "listening"; epBadge = null; ep = null; setRunning(false);
  $("badge").textContent = "중지됨"; $("badge").className = "badge"; $("rtt").textContent = "RTT - ms";
}
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
