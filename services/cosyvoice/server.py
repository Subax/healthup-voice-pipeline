"""CosyVoice3 스트리밍 HTTP 서버 (컨테이너 안에서 실행).

POST /tts {"text": "...", "voice": "calm"} -> PCM16 mono little-endian 청크 스트림 (모델 샘플레이트)
GET  /voices              -> {"default": id, "voices": [{"id", "label"}]}
GET  /info                -> {"sample_rate": N, "model": "..."}
한 번에 하나만 합성한다. 클라이언트가 끊으면 다음 청크 경계에서 생성을 멈춘다.
"""

from __future__ import annotations

import argparse
import faulthandler
import signal
import asyncio
import logging
import os
import sys
import threading
import time

import numpy as np

# model.tts(stream=True) 는 speed 를 token2wav 에 넘기지 않는다.
# 비스트림만 마지막 mel 길이를 줄인다. 그래서 나온 파형을 여기서 줄인다.
SPEECH_SPEED = float(os.environ.get("CV_SPEED", "1.2"))


class StreamSpeed:
    """청크를 이어서 읽어 길이를 1/speed 로 줄인다. 샘플레이트는 그대로다."""

    def __init__(self, speed: float = SPEECH_SPEED):
        self.speed = float(speed)
        self._buf = np.zeros(0, dtype=np.float64)
        self._origin = 0.0
        self._out_pos = 0.0

    def push(self, wav: np.ndarray) -> np.ndarray:
        if self.speed == 1.0:
            return np.asarray(wav)
        wav = np.asarray(wav, dtype=np.float64).reshape(-1)
        if wav.size == 0 and self._buf.size == 0:
            return np.zeros(0, dtype=np.float32)
        self._buf = np.concatenate([self._buf, wav])
        out: list[float] = []
        end = self._origin + self._buf.size - 1
        while self._out_pos <= end:
            i = int(np.floor(self._out_pos - self._origin))
            frac = self._out_pos - self._origin - i
            if i + 1 >= self._buf.size:
                break
            out.append((1.0 - frac) * self._buf[i] + frac * self._buf[i + 1])
            self._out_pos += self.speed
        keep = int(np.floor(self._out_pos - self._origin))
        if keep > 0:
            self._origin += keep
            self._buf = self._buf[keep:]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(out, dtype=np.float32)


import torch
if torch.cuda.is_available():
    torch.cuda.init()
    _ = torch.cuda.Stream()
import aiohttp
from aiohttp import web

class _SynthCancelled(Exception):
    """합성 도중 클라이언트 취소로 중단됨."""
    pass

# 속도 스위치(환경변수, 기본값이 측정으로 고른 설정). GB10 에서 cuDNN 을 켜면 HiFT f0_predictor(float64 conv)가
# "unable to find an engine" 으로 실패하고, 그 모듈만 끄고 켜도 전체가 더 느려서(RTF +50%) 기본은 끈다 (CV_CUDNN=1 로 켠다).
if os.environ.get("CV_CUDNN", "0") == "0":
    torch.backends.cudnn.enabled = False

if os.environ.get("CV_TF32", "1") == "1":
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.set_float32_matmul_precision("high")

sys.path[:0] = ["/opt/CosyVoice", "/opt/CosyVoice/third_party/Matcha-TTS"]

MODEL_DIR = os.environ.get("COSYVOICE_MODEL_DIR", "/models/Fun-CosyVoice3-0.5B-2512")
PROMPT_WAV = os.environ.get("COSYVOICE_PROMPT_WAV", "/opt/CosyVoice/asset/zero_shot_prompt.wav")
# CosyVoice3 공식 예제 형식: 시스템 문구 + <|endofprompt|> + 프롬프트 전사
PROMPT_TEXT = os.environ.get(
    "COSYVOICE_PROMPT_TEXT",
    "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。",
)
SYS = "You are a helpful assistant."
ASSET = "/opt/CosyVoice/asset"
WAV_Z = os.environ.get("COSYVOICE_PROMPT_WAV", f"{ASSET}/zero_shot_prompt.wav")

log = logging.getLogger("cosyvoice-server")


def _ko_wav(env: str, path: str) -> str:
    """한국어 참조 wav 가 없으면 경고하고 WAV_Z 로 대체한다 (서버는 계속 뜬다)."""
    p = os.environ.get(env, path)
    if not os.path.exists(p):
        log.warning("한국어 참조 wav 없음: %s -> WAV_Z 로 대체", p)
        return WAV_Z
    return p


# 한국어 여성 참조 음성 (models/voices, 24kHz mono)
KO_WAV_A = _ko_wav("COSYVOICE_KO_WAV_A", "/models/voices/ko_female_a.wav")
KO_TEXT_A = SYS + "<|endofprompt|>잠시 모든 걸 내려놓고, 깊은 숨을 내쉬어 보세요. 당신은 존재만으로도 충분히 귀하고 아름다운 사람입니다."
KO_WAV_B = _ko_wav("COSYVOICE_KO_WAV_B", "/models/voices/ko_female_b.wav")
KO_TEXT_B = SYS + "<|endofprompt|>당신에게 마음을 움직이는 목소리를 선물하고 싶습니다. 당신의 소중한 이야기들이 가장 아름다운 빛으로 피어날 수 있도록 제가 함께하겠습니다."

# 목소리 프리셋: mode = zero_shot(프롬프트 운율 복제) | instruct(말투 지시문 + 화자 음색만) | cross_lingual(프롬프트 없이 음색만).
# 지시문은 중국어·영어로 둔다. 한국어 지시문은 모델이 본문처럼 읽어 버린다 (2026-10-12, 40문장 CER 1.0 이상).
# text 는 zero_shot 이면 프롬프트 전사, instruct 이면 지시문이다 (모두 <|endofprompt|> 포함).
PRESETS: dict[str, dict] = {
    "calm": {"label": "차분하게", "wav": KO_WAV_A, "mode": "instruct",
             "text": SYS + " 请用温柔、平静的语气，非常轻声、缓慢地说话。<|endofprompt|>"},
    # 톤 프리셋(자동 톤 선택용, 같은 화자)
    "comfort": {"label": "위로하는", "wav": KO_WAV_A, "mode": "instruct",
                "text": SYS + " 请用温柔、体贴的语气，轻轻地、慢慢地说话。<|endofprompt|>"},
    "warm": {"label": "따뜻하게", "wav": KO_WAV_A, "mode": "instruct",
             "text": SYS + " 请用温和、愉快的语气，自然地说话，声音温暖而不夸张。<|endofprompt|>"},
    "steady": {"label": "차분하고 단단하게", "wav": KO_WAV_A, "mode": "instruct",
               "text": SYS + " Please speak in a calm, low, steady voice, slowly and gently.<|endofprompt|>"},
    "ko_a": {"label": "한국어 참조 A (운율 복제)", "wav": KO_WAV_A, "mode": "zero_shot", "text": KO_TEXT_A},
    "ko_b": {"label": "한국어 참조 B (운율 복제)", "wav": KO_WAV_B, "mode": "zero_shot", "text": KO_TEXT_B},
    # A/B 비교용: 기존 중국어 참조 + 중국어 지시문
    "zh_calm": {"label": "차분하게 (기존 중국어 참조)", "wav": WAV_Z, "mode": "instruct",
                "text": SYS + " 请用温柔、平静的语气，非常轻声、缓慢地说话。<|endofprompt|>"},
}
DEFAULT_VOICE = os.environ.get("CV_VOICE", "calm")
if DEFAULT_VOICE not in PRESETS:
    DEFAULT_VOICE = "calm"


def _fast_ras_sampling(scores, decoded_tokens, sampling, top_p=0.8, top_k=25, win_size=10, tau_r=0.1):
    """cosyvoice.utils.common.ras_sampling 과 같은 분포를 GPU 벡터 연산으로 계산한다.
    원본 nucleus_sampling 은 파이썬 루프에서 GPU 스칼라를 읽어 토큰마다 수십 번 동기화한다."""
    probs = scores.softmax(dim=0)
    sv, si = probs.sort(descending=True, stable=True)
    k = min(top_k, sv.numel())
    sv, si = sv[:k], si[:k]
    excl = sv.cumsum(0) - sv  # 자기 앞까지의 누적 확률 < top_p 인 항목만 포함
    w = torch.where(excl < top_p, sv, torch.zeros_like(sv))
    top = int(si[w.multinomial(1, replacement=True)].item())
    rep = sum(1 for t in decoded_tokens[-win_size:] if t == top)
    if rep >= win_size * tau_r:
        scores[top] = -float("inf")
        top = int(scores.softmax(dim=0).multinomial(1, replacement=True).item())
    return top


class _ModelTimeProxy:
    """cosyvoice.cli.model 전용 time 프록시.

    전역 time.sleep 오염 없이 model.tts 내부의 time.sleep(0.1) 폴링 대기만
    CV_POLL_MS(기본 10ms)로 단축한다.
    취소 이벤트(_cur_cancel) 대기 후 즉시 반환하여 예외 없이 2437f38 정리 경로를 탄다.
    """

    def __init__(self, engine: Engine, orig_time: Any):
        self._engine = engine
        self._orig_time = orig_time

    def sleep(self, s: float) -> None:
        if abs(s - 0.1) < 1e-3:
            poll_s = self._engine.poll_ms / 1000.0
            self._engine._cur_cancel.wait(timeout=poll_s)
            return
        self._orig_time.sleep(s)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig_time, name)


class Engine:
    def __init__(self, model_dir: str, fp16: bool = False):
        import cosyvoice.cli.model as cv_model_mod
        from cosyvoice.cli.cosyvoice import AutoModel

        self.poll_ms: float = float(os.environ.get("CV_POLL_MS", "10"))
        cv_model_mod.time = _ModelTimeProxy(self, time)

        self.cv = AutoModel(model_dir=model_dir, fp16=fp16)
        self.sample_rate: int = self.cv.sample_rate
        self.model_dir = model_dir
        self._prof: dict[str, Any] = {}
        self._job_lock = threading.Lock()
        self._active_llm_jobs: dict[str, threading.Event] = {}
        self._cur_cancel: threading.Event = threading.Event()
        # model.tts 는 스트리밍 중 token_hop_len 을 키우고 되돌리지 않는다 -> 요청마다 초기값으로 리셋
        # 첫 오디오까지 모으는 토큰 수. 25(공식 기본) → 10: 단독 TTFA P50 787 → 507ms, 왕복 CER 같음 (2026-10-21,
        # bench/hop_sweep.sh). 6은 TTFA가 더 줄지 않고 CER·RTF가 나빠졌다. 청취 확인은 하지 않았다
        self.cv.model.token_hop_len = int(os.environ.get("CV_HOP", "10"))
        self.hop0: int = self.cv.model.token_hop_len
        self._patch_llm()
        self._patch_llm_job()
        if os.environ.get("CV_PROF"):
            m = self.cv.model
            o2w = m.token2wav

            def t2w(*a, **kw):
                torch.cuda.synchronize()
                t0 = time.monotonic()
                r = o2w(*a, **kw)
                torch.cuda.synchronize()
                log.info("PROF token2wav %.0fms ntok=%d", (time.monotonic() - t0) * 1000, kw["token"].shape[1])
                return r

            m.token2wav = t2w
            for name, mod in (("flow", m.flow), ("hift", m.hift)):
                o = mod.inference

                def mk(o, name):
                    def f(*a, **kw):
                        torch.cuda.synchronize()
                        t0 = time.monotonic()
                        r = o(*a, **kw)
                        torch.cuda.synchronize()
                        log.info("PROF   %s %.0fms", name, (time.monotonic() - t0) * 1000)
                        return r
                    return f

                mod.inference = mk(o, name)
        self._patch_f0()
        self._patch_cancel()
        if os.environ.get("CV_FASTSAMP", "1") == "1":
            self.cv.model.llm.sampling = _fast_ras_sampling
        if os.environ.get("CV_GRAPH", "1") == "1":
            self._patch_llm_graph()
        if os.environ.get("CV_STEPS", "5"):
            # flow DiT 의 Euler 스텝 수 (기본 10). 줄이면 빨라지고 음질은 CER 로 확인한다
            dec = self.cv.model.flow.decoder
            o_fwd, n_steps = dec.forward, int(os.environ.get("CV_STEPS", "5"))
            dec.forward = lambda *a, **kw: o_fwd(*a, **{**kw, "n_timesteps": n_steps})
        # 프롬프트 wav 별 화자 특징(음성 토큰·멜·임베딩)을 한 번만 계산해 둔다
        self.presets = PRESETS
        self._base: dict[str, dict] = {}
        for pr in self.presets.values():
            self._prompt_base(pr["wav"])

    def _patch_llm_graph(self) -> None:
        """LLM 디코드 1스텝을 CUDA graph 로 재생한다 (StaticCache). 2~3개 모델이 GPU 를 time-slice 로 나눌 때
        커널 launch 사이 CPU 공백마다 슬라이스를 뺏기므로, launch 수를 줄이는 것이 가장 효과가 크다."""
        from transformers import StaticCache

        lm = self.cv.model.llm
        qwen = lm.llm.model.model  # Qwen2Model
        cfg = lm.llm.model.config
        dev = self.cv.model.device
        max_len = int(os.environ.get("CV_GRAPH_MAXLEN", "3072"))
        if os.environ.get("CV_LLM_DTYPE", "fp16") in ("bf16", "fp16"):
            ldt = torch.bfloat16 if os.environ.get("CV_LLM_DTYPE", "fp16") == "bf16" else torch.float16
            qwen.to(ldt)
            lm.llm_decoder.to(ldt)
        dtype = next(qwen.parameters()).dtype
        cache = StaticCache(config=cfg, max_batch_size=1, max_cache_len=max_len, device=dev, dtype=dtype)
        pos = torch.zeros(1, dtype=torch.long, device=dev)

        x = torch.zeros(1, 1, lm.llm_input_size, device=dev, dtype=dtype)

        def step(inp, cpos):
            h = qwen(inputs_embeds=inp, past_key_values=cache, cache_position=cpos, use_cache=True).last_hidden_state
            return lm.llm_decoder(h[:, -1]).float().log_softmax(dim=-1)

        with torch.inference_mode():
            st = torch.cuda.Stream()
            st.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(st):
                for _ in range(3):
                    step(x, pos)
            torch.cuda.current_stream().wait_stream(st)
            g = torch.cuda.CUDAGraph()
            with torch.cuda.graph(g):
                out = step(x, pos)
        orig_wrapper = lm.inference_wrapper

        @torch.inference_mode()
        def wrapper(lm_input, sampling, min_len, max_len_tok, uuid):
            cur_cancel = self._cur_cancel
            L = lm_input.shape[1]
            if L + max_len_tok >= max_len:
                yield from orig_wrapper(lm_input, sampling, min_len, max_len_tok, uuid)
                return
            t_start = time.monotonic()
            n_tok = 0
            t_first = None
            prompt_token_pad = 3  # calm(KO_WAV_A): 10 hop + 3 pad + 3 pre_lookahead = 16
            need_tok = self.cv.model.token_hop_len + getattr(self.cv.model.flow, "pre_lookahead_len", 3) + prompt_token_pad
            h = qwen(inputs_embeds=lm_input.to(dtype), past_key_values=cache,
                     cache_position=torch.arange(L, device=dev), use_cache=True).last_hidden_state
            logp = lm.llm_decoder(h[:, -1]).float().log_softmax(dim=-1)
            out_tokens: list[int] = []
            for i in range(max_len_tok):
                if cur_cancel.is_set():
                    break
                top = lm.sampling_ids(logp.squeeze(0), out_tokens, sampling, ignore_eos=i < min_len)
                if top in lm.stop_token_ids:
                    break
                now = time.monotonic()
                n_tok += 1
                if t_first is None:
                    t_first = now - t_start
                    if self._prof and "t_tts_enter" in self._prof and "llm_first_tok_ms" not in self._prof:
                        self._prof["llm_first_tok_ms"] = (now - self._prof["t_tts_enter"]) * 1000
                if self._prof and n_tok >= need_tok and "t_hop1_ready" not in self._prof:
                    self._prof["t_hop1_ready"] = now
                    if "t_tts_enter" in self._prof:
                        self._prof["hop1_ready_ms"] = (now - self._prof["t_tts_enter"]) * 1000
                yield top
                out_tokens.append(top)
                x.copy_(lm.speech_embedding.weight[top].reshape(1, 1, -1).to(dtype))
                pos.fill_(L + i)
                g.replay()
                logp = out

            now_end = time.monotonic()
            if self._prof:
                if "t_hop1_ready" not in self._prof:
                    self._prof["t_hop1_ready"] = now_end
                    if "t_tts_enter" in self._prof:
                        self._prof["hop1_ready_ms"] = (now_end - self._prof["t_tts_enter"]) * 1000
            if os.environ.get("CV_PROF"):
                total_ms = (now_end - t_start) * 1000
                first_ms = (t_first or 0) * 1000
                ms_per_tok = (total_ms - first_ms) / max(n_tok - 1, 1) if n_tok > 1 else 0.0
                log.info("PROF llm tokens=%d total=%.0fms first=%.0fms (%.1fms/tok)", n_tok, total_ms, first_ms, ms_per_tok)

        lm.inference_wrapper = wrapper

    @property
    def cancel(self) -> threading.Event:
        return self._cur_cancel

    def _patch_llm_job(self) -> None:
        """llm_job 스레드 생명주기를 추적하여 취소 뒤 정상 종료를 대기할 수 있게 한다."""
        orig_llm_job = self.cv.model.llm_job

        def wrapped_llm_job(*args, **kwargs):
            uuid_val = kwargs.get("uuid") if "uuid" in kwargs else (args[4] if len(args) > 4 else id(threading.current_thread()))
            done_ev = threading.Event()
            with self._job_lock:
                self._active_llm_jobs[str(uuid_val)] = done_ev
            try:
                return orig_llm_job(*args, **kwargs)
            finally:
                done_ev.set()
                with self._job_lock:
                    self._active_llm_jobs.pop(str(uuid_val), None)

        self.cv.model.llm_job = wrapped_llm_job

    def _patch_cancel(self) -> None:
        """token2wav, flow.inference, hift.inference 가 cancel 플래그를 보게 감싸 취소 시 잔여 연산을 즉시 중단한다."""
        orig_token2wav = self.cv.model.token2wav
        orig_flow_inf = self.cv.model.flow.inference
        orig_hift_inf = self.cv.model.hift.inference

        def cancel_aware_token2wav(*a, **kw):
            if self._cur_cancel.is_set():
                raise _SynthCancelled()
            if self._prof and "t_t2w_first" not in self._prof:
                t_enter = time.monotonic()
                self._prof["t_t2w_first"] = t_enter
                t_hop = self._prof.get("t_hop1_ready", t_enter)
                self._prof["poll_wait_ms"] = max(0.0, (t_enter - t_hop) * 1000)
                self._prof["finalize"] = kw.get("finalize", False)
                res = orig_token2wav(*a, **kw)
                self._prof["token2wav_ms"] = (time.monotonic() - t_enter) * 1000
                return res
            return orig_token2wav(*a, **kw)

        def cancel_aware_flow_inf(*a, **kw):
            if self._cur_cancel.is_set():
                raise _SynthCancelled()
            return orig_flow_inf(*a, **kw)

        def cancel_aware_hift_inf(*a, **kw):
            if self._cur_cancel.is_set():
                raise _SynthCancelled()
            return orig_hift_inf(*a, **kw)

        self.cv.model.token2wav = cancel_aware_token2wav
        self.cv.model.flow.inference = cancel_aware_flow_inf
        self.cv.model.hift.inference = cancel_aware_hift_inf

    def _patch_f0(self) -> None:
        """HiFT f0_predictor 는 float64 conv 를 쓰는데 GB10 cuDNN 에 fp64 엔진이 없다. 이 모듈만 cuDNN 을 끈다."""
        f0 = self.cv.model.hift.f0_predictor
        orig = f0.forward

        def fwd(*a, **kw):
            with torch.backends.cudnn.flags(enabled=False):
                return orig(*a, **kw)

        f0.forward = fwd

    def _patch_llm(self) -> None:
        """llm.inference 가 cancel 플래그를 보게 감싼다 (연결이 끊기면 토큰 생성을 멈춘다)."""
        llm = self.cv.model.llm
        orig = llm.inference

        def wrapped(*a, **kw):
            cur_cancel = self._cur_cancel
            it = iter(orig(*a, **kw))
            n, tt, t_first = 0, 0.0, None
            prompt_token_pad = 3  # calm(KO_WAV_A): 10 hop + 3 pad + 3 pre_lookahead = 16
            need_tok = self.cv.model.token_hop_len + getattr(self.cv.model.flow, "pre_lookahead_len", 3) + prompt_token_pad
            while True:
                t0 = time.monotonic()
                try:
                    tok = next(it)
                except StopIteration:
                    break
                dt = time.monotonic() - t0
                tt += dt
                n += 1
                if t_first is None:
                    t_first = dt
                    if self._prof and "t_tts_enter" in self._prof and "llm_first_tok_ms" not in self._prof:
                        self._prof["llm_first_tok_ms"] = (time.monotonic() - self._prof["t_tts_enter"]) * 1000
                if self._prof and n >= need_tok and "t_hop1_ready" not in self._prof:
                    self._prof["t_hop1_ready"] = time.monotonic()
                    if "t_tts_enter" in self._prof:
                        self._prof["hop1_ready_ms"] = (self._prof["t_hop1_ready"] - self._prof["t_tts_enter"]) * 1000
                if cur_cancel.is_set():
                    break
                yield tok
            if self._prof:
                if "t_hop1_ready" not in self._prof:
                    self._prof["t_hop1_ready"] = time.monotonic()
                    if "t_tts_enter" in self._prof:
                        self._prof["hop1_ready_ms"] = (self._prof["t_hop1_ready"] - self._prof["t_tts_enter"]) * 1000
            if os.environ.get("CV_PROF"):
                log.info("PROF llm tokens=%d total=%.0fms first=%.0fms (%.1fms/tok)", n, tt * 1000, (t_first or 0) * 1000, tt * 1000 / max(n, 1))

        llm.inference = wrapped

    def _prompt_base(self, wav: str) -> dict:
        if wav not in self._base:
            fe = self.cv.frontend
            self._base[wav] = fe.frontend_zero_shot("", "", wav, self.sample_rate, "")
        return self._base[wav]

    def _model_input(self, preset: dict, seg: str) -> dict:
        """프리셋 모드별로 model.tts 입력을 만든다 (프레임워크 frontend_* 와 같은 구성)."""
        fe = self.cv.frontend
        mi = dict(self._prompt_base(preset["wav"]))
        mi.pop("text", None), mi.pop("text_len", None)
        mi["text"], mi["text_len"] = fe._extract_text_token(seg)
        mode = preset["mode"]
        if mode == "zero_shot":
            mi["prompt_text"], mi["prompt_text_len"] = fe._extract_text_token(preset["text"])
        elif mode == "instruct":  # 지시문을 prompt_text 로, LLM 쪽 음성 프롬프트는 뺀다 (음색은 flow 가 입힌다)
            mi["prompt_text"], mi["prompt_text_len"] = fe._extract_text_token(preset["text"])
            mi.pop("llm_prompt_speech_token"), mi.pop("llm_prompt_speech_token_len")
        else:  # cross_lingual
            for k in ("prompt_text", "prompt_text_len", "llm_prompt_speech_token", "llm_prompt_speech_token_len"):
                mi.pop(k, None)
        return mi

    def synth(self, text: str, voice: str | None = None, cancel: threading.Event | None = None):
        """PCM16 바이트 청크 제너레이터."""
        cancel = cancel or threading.Event()
        self._cur_cancel = cancel
        self.cv.model.token_hop_len = self.hop0
        if self._prof:
            self._prof["t_synth"] = time.monotonic()
        preset = self.presets.get(voice or "", self.presets[DEFAULT_VOICE])
        if preset["mode"] == "cross_lingual":
            text = SYS + "<|endofprompt|>" + text
        # wetext 영어 정규화가 한글+숫자 문장에서 AssertionError 를 낸다 -> 프론트엔드 없이 재시도
        try:
            segs = self.cv.frontend.text_normalize(text, split=True, text_frontend=True)
        except Exception:  # noqa: BLE001
            log.warning("text_frontend 실패, 정규화 없이 합성: %s", text)
            segs = self.cv.frontend.text_normalize(text, split=True, text_frontend=False)
        speeder = StreamSpeed(SPEECH_SPEED)
        try:
            for seg in segs:
                if cancel.is_set():
                    break
                self.cv.model.token_hop_len = self.hop0
                t_enter = time.monotonic()
                if self._prof and "t_tts_enter" not in self._prof:
                    self._prof["t_tts_enter"] = t_enter
                    self._prof["frontend_ms"] = (t_enter - self._prof.get("t_synth", t_enter)) * 1000
                # stream=True 경로는 speed 인자를 무시한다. 파형 길이는 StreamSpeed 가 줄인다.
                gen = self.cv.model.tts(**self._model_input(preset, seg), stream=True, speed=1.0)
                try:
                    for out in gen:
                        if cancel.is_set():
                            break
                        wav = speeder.push(out["tts_speech"].squeeze(0).numpy())
                        if wav.size == 0:
                            continue
                        yield (np.clip(wav, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                except _SynthCancelled:
                    log.info("합성 취소 중단 (token2wav/flow/hift 차단)")
                    break
                finally:
                    gen.close()
        finally:
            cancel.set()
            # (2) llm_job 종료를 이벤트로 대기
            with self._job_lock:
                events = list(self._active_llm_jobs.values())
            for ev in events:
                if not ev.wait(timeout=1.0):
                    log.warning("llm_job 종료 대기 타임아웃 (1.0s)")
            # (3) 사전 정리는 서버가 직접 수행 (잔여 키 수 로깅)
            with self.cv.model.lock:
                rem_tok = len(self.cv.model.tts_speech_token_dict)
                rem_end = len(self.cv.model.llm_end_dict)
                rem_hift = len(self.cv.model.hift_cache_dict)
                if rem_tok or rem_end or rem_hift:
                    log.info("정리 전 잔여 키: tok=%d end=%d hift=%d", rem_tok, rem_end, rem_hift)
                self.cv.model.tts_speech_token_dict.clear()
                self.cv.model.llm_end_dict.clear()
                self.cv.model.hift_cache_dict.clear()
            if torch.cuda.is_available():
                torch.cuda.synchronize()


async def handle_info(request: web.Request) -> web.Response:
    eng: Engine = request.app["engine"]
    return web.json_response({"sample_rate": eng.sample_rate, "model": os.path.basename(eng.model_dir),
                              "default_voice": DEFAULT_VOICE})


async def handle_voices(request: web.Request) -> web.Response:
    eng: Engine = request.app["engine"]
    return web.json_response({"default": DEFAULT_VOICE,
                              "voices": [{"id": k, "label": v["label"]} for k, v in eng.presets.items()]})


async def handle_tts(request: web.Request) -> web.StreamResponse:
    eng: Engine = request.app["engine"]
    body = await request.json()
    text = body.get("text", "").strip()
    voice = body.get("voice")
    if os.environ.get("CV_DEV") == "1" and voice in eng.presets and isinstance(body.get("dev"), dict):
        # 튜닝용(기본 꺼짐): 기존 프리셋의 모드·지시문만 요청별로 바꾼다. wav 는 프리셋 것을 그대로 쓴다
        d = body["dev"]
        eng.presets["_dev"] = {**eng.presets[voice], "mode": str(d.get("mode", eng.presets[voice]["mode"])),
                               "text": str(d.get("text", eng.presets[voice]["text"]))}
        voice = "_dev"
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    loop = asyncio.get_running_loop()
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await resp.prepare(request)
    async with request.app["lock"]:  # 한 번에 하나만 합성
        if request.transport is None or request.transport.is_closing():
            return resp
        cancel_ev = threading.Event()
        q: asyncio.Queue = asyncio.Queue()
        t0 = time.monotonic()
        eng._prof = {"t0": t0} if os.environ.get("CV_PROF") else {}

        def work() -> None:
            try:
                for pcm in eng.synth(text, voice, cancel=cancel_ev):
                    loop.call_soon_threadsafe(q.put_nowait, pcm)
            except Exception as e:  # noqa: BLE001
                log.exception("synth 실패")
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        th = threading.Thread(target=work, daemon=True)
        th.start()
        first = True
        try:
            while True:
                # 클라이언트 연결 끊김 즉시 감지 (is_closing)
                if request.transport is None or request.transport.is_closing():
                    log.info("취소 감지 (is_closing)")
                    cancel_ev.set()
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.04)
                except asyncio.TimeoutError:
                    continue

                if item is None:
                    break
                if isinstance(item, Exception):
                    break
                if first:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                    log.info("첫 청크 %.0fms", first_chunk_ms)
                    first = False
                    if os.environ.get("CV_PROF"):
                        f_ms = eng._prof.get("frontend_ms", 0.0)
                        l1_ms = eng._prof.get("llm_first_tok_ms", 0.0)
                        h1_ms = eng._prof.get("hop1_ready_ms", 0.0)
                        pw_ms = eng._prof.get("poll_wait_ms", 0.0)
                        t2w_ms = eng._prof.get("token2wav_ms", 0.0)
                        fin = eng._prof.get("finalize", False)
                        log.info(
                            "PROF_BREAKDOWN frontend=%.1fms llm_first_tok=%.1fms hop1_ready=%.1fms poll_wait=%.1fms token2wav=%.1fms first_chunk=%.1fms finalize=%s",
                            f_ms, l1_ms, h1_ms, pw_ms, t2w_ms, first_chunk_ms, fin,
                        )
                try:
                    await resp.write(item)
                except (ConnectionResetError, aiohttp.ClientError):
                    log.info("취소 감지 (write error)")
                    cancel_ev.set()
                    break
            if not (request.transport is None or request.transport.is_closing()):
                try:
                    await resp.write_eof()
                except Exception:
                    pass
        except (ConnectionResetError, asyncio.CancelledError, aiohttp.ClientError):
            cancel_ev.set()
        finally:
            cancel_ev.set()  # 정상/비정상 종료 모두 남은 작업 정리
            await loop.run_in_executor(None, th.join)  # 다음 요청 전에 스레드 종료 보장
    return resp


def build_app(engine: Engine) -> web.Application:
    app = web.Application()
    app["engine"] = engine
    app["lock"] = asyncio.Lock()
    app.router.add_get("/info", handle_info)
    app.router.add_get("/voices", handle_voices)
    app.router.add_post("/tts", handle_tts)
    return app


def main() -> None:
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # 멈췄을 때 kill -USR1 1 로 스택 확인
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--fp16", action="store_true")
    args = ap.parse_args()
    args.fp16 = args.fp16 or os.environ.get("CV_FP16", "0") == "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine = Engine(args.model_dir, fp16=args.fp16)
    t0 = time.monotonic()
    n = 0
    for vid in engine.presets:  # 프리셋마다 한 번씩 워밍업 (모드별 경로·CUDA graph 확인)
        n += sum(len(p) for p in engine.synth("안녕하세요, 워밍업입니다.", vid))
    log.info("warmup %.1fs, %d bytes, sr=%d", time.monotonic() - t0, n, engine.sample_rate)
    web.run_app(build_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
