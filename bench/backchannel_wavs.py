"""맞장구 wav 청취용 생성: 모든 목소리로 합성해 bench/results/backchannel/ 에 저장한다.

사용법:
    .venv/bin/python bench/backchannel_wavs.py [--out bench/results/backchannel]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "src")

from voice_pipeline.backchannel import BackchannelBank  # noqa: E402
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS  # noqa: E402


async def main(out: str) -> None:
    tts = CosyVoiceTTS()
    await tts.warmup()
    voices = [v["id"] for v in tts.voices] or [tts.voice]
    bank = BackchannelBank()
    await bank.prepare(tts, voices)
    for p in bank.save_wavs(out, tts.sample_rate):
        ms = (p.stat().st_size - 44) / 2 / tts.sample_rate * 1000
        print(f"{p}  {ms:.0f} ms")
    await tts.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/results/backchannel")
    asyncio.run(main(ap.parse_args().out))
