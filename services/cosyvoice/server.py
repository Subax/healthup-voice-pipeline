"""CosyVoice3 스트리밍 HTTP 서버 (컨테이너 안에서 실행).

POST /tts {"text": "..."} -> PCM16 mono little-endian 청크 스트림 (모델 샘플레이트)
GET  /info                -> {"sample_rate": N, "model": "..."}
한 번에 하나만 합성한다. 클라이언트가 끊으면 다음 청크 경계에서 생성을 멈춘다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time

import numpy as np
import torch
from aiohttp import web

# GB10(sm_121)에서 cuDNN conv 엔진을 못 찾는 문제("unable to find an engine")를 피한다
torch.backends.cudnn.enabled = False

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


class Engine:
    def __init__(self, model_dir: str, fp16: bool = False):
        from cosyvoice.cli.cosyvoice import AutoModel

        self.cv = AutoModel(model_dir=model_dir, fp16=fp16)
        self.sample_rate: int = self.cv.sample_rate
        self.model_dir = model_dir
        self.cancel = threading.Event()
        # model.tts 는 스트리밍 중 token_hop_len 을 키우고 되돌리지 않는다 -> 요청마다 초기값으로 리셋
        self.hop0: int = self.cv.model.token_hop_len
        self._patch_llm()
        # 화자 프롬프트 임베딩을 미리 계산해 둔다
        self.cv.add_zero_shot_spk(PROMPT_TEXT, PROMPT_WAV, SPK_ID)

    def _patch_llm(self) -> None:
        """llm.inference 가 cancel 플래그를 보게 감싼다 (연결이 끊기면 토큰 생성을 멈춘다)."""
        llm = self.cv.model.llm
        orig = llm.inference

        def wrapped(*a, **kw):
            for tok in orig(*a, **kw):
                if self.cancel.is_set():
                    return
                yield tok

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--fp16", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine = Engine(args.model_dir, fp16=args.fp16)
    t0 = time.monotonic()
    n = sum(len(p) for p in engine.synth("안녕하세요, 워밍업입니다."))  # 워밍업
    log.info("warmup %.1fs, %d bytes, sr=%d", time.monotonic() - t0, n, engine.sample_rate)
    web.run_app(build_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
