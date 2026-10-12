"use strict";
const $ = (id) => document.getElementById(id);
const JITTER = 0.06;        // 초기 지터 버퍼 (초)
const PROGRESS_MS = 100;    // 재생 위치 보고 주기

let ws = null, ctx = null, master = null;
let cur = null;             // 현재 재생 중인 턴
const deadTurns = new Set();
let speakingLocal = false, lastState = "listening";
const turnEls = {};         // turn_id -> 말풍선
const latRows = {};

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

// ---------- UI ----------
function setBadge() {
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
  const cells = [`#${turn_id}`, tr.stt_ms, tr.llm_first_token, tr.first_chunk, tr.backchannel, tr.tts_first_audio, tr.playback_start];
  tr_el.innerHTML = cells.map((c, i) => `<td>${i ? fmt(c) : c}</td>`).join("");
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
  if (m.backchannel != null) $("backchannel").checked = m.backchannel;
}
function onMessage(ev) {
  if (typeof ev.data !== "string") { if (ctx) onFrame(ev.data); return; }
  const m = JSON.parse(ev.data);
  switch (m.type) {
    case "hello": onHello(m); break;
    case "avatar_state": lastState = m.state; setBadge(); break;
    case "vad": $("vad").classList.toggle("on", m.speaking); break;
    case "transcript": addMsg("user", m.text + (m.emotion || m.event ? `  [${[m.emotion, m.event].filter(Boolean).join(", ")}]` : "")); break;
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
async function start() {
  $("start").disabled = true;
  ctx = new AudioContext({ latencyHint: "interactive" });
  master = ctx.createGain(); master.connect(ctx.destination);
  await ctx.resume();
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.binaryType = "arraybuffer";
  ws.onmessage = onMessage;
  ws.onclose = () => { $("badge").textContent = "연결 끊김"; $("badge").className = "badge"; };
  await new Promise((res) => { ws.onopen = res; });
  send({ type: "config", barge_in: $("bargein").checked, backchannel: $("backchannel").checked });
  lastState = "listening"; setBadge();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    await ctx.audioWorklet.addModule("/static/mic-worklet.js");
    const node = new AudioWorkletNode(ctx, "mic-processor");
    const mute = ctx.createGain(); mute.gain.value = 0;
    ctx.createMediaStreamSource(stream).connect(node); node.connect(mute); mute.connect(ctx.destination);
    node.port.onmessage = (e) => {
      const pcm = new Int16Array(e.data);
      let s = 0; for (let i = 0; i < pcm.length; i++) s += pcm[i] * pcm[i];
      const db = 20 * Math.log10(Math.sqrt(s / pcm.length) / 32768 + 1e-6);
      $("level").style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + "%";
      if (ws.readyState === 1 && ws.bufferedAmount < 64000) ws.send(e.data);
    };
  } catch (e) {
    $("err").textContent = "마이크를 쓸 수 없습니다(" + e.message + "). 텍스트 입력으로 시험하세요. localhost 또는 HTTPS 주소여야 합니다.";
  }
}

$("start").onclick = start;
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
