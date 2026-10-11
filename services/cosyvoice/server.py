"""CosyVoice3 스트리밍 HTTP 서버 (컨테이너 안에서 실행).

POST /tts {"text": "..."} -> PCM16 mono little-endian 청크 스트림 (모델 샘플레이트)
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
import torch
from aiohttp import web

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
SPK_ID = "default"

log = logging.getLogger("cosyvoice-server")


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


class Engine:
    def __init__(self, model_dir: str, fp16: bool = False):
        from cosyvoice.cli.cosyvoice import AutoModel

        self.cv = AutoModel(model_dir=model_dir, fp16=fp16)
        self.sample_rate: int = self.cv.sample_rate
        self.model_dir = model_dir
        self.cancel = threading.Event()
        # model.tts 는 스트리밍 중 token_hop_len 을 키우고 되돌리지 않는다 -> 요청마다 초기값으로 리셋
        if os.environ.get("CV_HOP"):
            self.cv.model.token_hop_len = int(os.environ["CV_HOP"])
        self.hop0: int = self.cv.model.token_hop_len
        self._patch_llm()
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
        if os.environ.get("CV_FASTSAMP", "1") == "1":
            self.cv.model.llm.sampling = _fast_ras_sampling
        if os.environ.get("CV_GRAPH", "1") == "1":
            self._patch_llm_graph()
        if os.environ.get("CV_STEPS", "5"):
            # flow DiT 의 Euler 스텝 수 (기본 10). 줄이면 빨라지고 음질은 CER 로 확인한다
            dec = self.cv.model.flow.decoder
            o_fwd, n_steps = dec.forward, int(os.environ.get("CV_STEPS", "5"))
            dec.forward = lambda *a, **kw: o_fwd(*a, **{**kw, "n_timesteps": n_steps})
        # 화자 프롬프트 임베딩을 미리 계산해 둔다
        self.cv.add_zero_shot_spk(PROMPT_TEXT, PROMPT_WAV, SPK_ID)

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
            L = lm_input.shape[1]
            if L + max_len_tok >= max_len:
                yield from orig_wrapper(lm_input, sampling, min_len, max_len_tok, uuid)
                return
            h = qwen(inputs_embeds=lm_input.to(dtype), past_key_values=cache,
                     cache_position=torch.arange(L, device=dev), use_cache=True).last_hidden_state
            logp = lm.llm_decoder(h[:, -1]).float().log_softmax(dim=-1)
            out_tokens: list[int] = []
            for i in range(max_len_tok):
                top = lm.sampling_ids(logp.squeeze(0), out_tokens, sampling, ignore_eos=i < min_len)
                if top in lm.stop_token_ids:
                    break
                yield top
                out_tokens.append(top)
                x.copy_(lm.speech_embedding.weight[top].reshape(1, 1, -1).to(dtype))
                pos.fill_(L + i)
                g.replay()
                logp = out

        lm.inference_wrapper = wrapper

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
            it = iter(orig(*a, **kw))
            n, tt, t_first = 0, 0.0, None
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
                if self.cancel.is_set():
                    break
                yield tok
            if os.environ.get("CV_PROF"):
                log.info("PROF llm tokens=%d total=%.0fms first=%.0fms (%.1fms/tok)", n, tt * 1000, (t_first or 0) * 1000, tt * 1000 / max(n, 1))

        llm.inference = wrapped

    def synth(self, text: str):
        """PCM16 바이트 청크 제너레이터. 호출 전에 cancel 을 비운다."""
        self.cancel.clear()
        self.cv.model.token_hop_len = self.hop0
        # wetext 영어 정규화가 한글+숫자 문장에서 AssertionError 를 낸다 -> 프론트엔드 없이 재시도
        try:
            self.cv.frontend.text_normalize(text, split=True, text_frontend=True)
            frontend = True
        except Exception:  # noqa: BLE001
            log.warning("text_frontend 실패, 정규화 없이 합성: %s", text)
            frontend = False
        gen = self.cv.inference_zero_shot(text, "", "", zero_shot_spk_id=SPK_ID, stream=True, text_frontend=frontend)
        try:
            for out in gen:
                if self.cancel.is_set():
                    break
                wav = out["tts_speech"].squeeze(0).numpy()
                yield (np.clip(wav, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        finally:
            gen.close()


async def handle_info(request: web.Request) -> web.Response:
    eng: Engine = request.app["engine"]
    return web.json_response({"sample_rate": eng.sample_rate, "model": os.path.basename(eng.model_dir)})


async def handle_tts(request: web.Request) -> web.StreamResponse:
    eng: Engine = request.app["engine"]
    text = (await request.json()).get("text", "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    loop = asyncio.get_running_loop()
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await resp.prepare(request)
    async with request.app["lock"]:  # 한 번에 하나만 합성
        q: asyncio.Queue = asyncio.Queue()
        t0 = time.monotonic()

        def work() -> None:
            try:
                for pcm in eng.synth(text):
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
            while (item := await q.get()) is not None:
                if isinstance(item, Exception):
                    break
                if first:
                    log.info("첫 청크 %.0fms", (time.monotonic() - t0) * 1000)
                    first = False
                await resp.write(item)
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            eng.cancel.set()  # 클라이언트가 끊었다
            raise
        finally:
            eng.cancel.set()  # 정상 종료 시에도 남은 작업 정리
            await loop.run_in_executor(None, th.join)  # 다음 요청 전에 스레드 종료 보장
    return resp


def build_app(engine: Engine) -> web.Application:
    app = web.Application()
    app["engine"] = engine
    app["lock"] = asyncio.Lock()
    app.router.add_get("/info", handle_info)
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
    n = sum(len(p) for p in engine.synth("안녕하세요, 워밍업입니다."))  # 워밍업
    log.info("warmup %.1fs, %d bytes, sr=%d", time.monotonic() - t0, n, engine.sample_rate)
    web.run_app(build_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
