"""TTS 후보 벤치 (개발 포인트 문서 §4.4 결정 규칙 1).

같은 문장 세트로 TTFA(첫 오디오 지연)와 RTF(합성 시간 ÷ 오디오 길이)를 잰다.
청취 점수는 --save-wav로 저장한 파일을 사람이 듣고 따로 매긴다.

실행 예:
    python bench/tts_bench.py --engine mock
    python bench/tts_bench.py --engine mock --repeat 3 --save-wav bench/results/mock

새 엔진은 ENGINES에 팩토리를 추가한다. 엔진은 voice_pipeline.tts.base.TTSEngine을 따른다.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_pipeline.normalize import normalize_for_tts  # noqa: E402
from voice_pipeline.trace import percentile  # noqa: E402
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS  # noqa: E402
from voice_pipeline.tts.mock import MockTTS  # noqa: E402


def _not_yet(name: str):
    def factory():
        raise SystemExit(f"'{name}' 어댑터는 아직 없다 (⏭️). voice_pipeline/tts/ 에 구현한 뒤 등록한다.")
    return factory


ENGINES = {
    "mock": lambda: MockTTS(first_audio_ms=80, rtf=0.1),
    "omnivoice": _not_yet("omnivoice"),
    "cosyvoice3": lambda: CosyVoiceTTS(),
    "azure": _not_yet("azure"),
    "clova": _not_yet("clova"),
    "qwen3": _not_yet("qwen3"),
}


def load_sentences(path: pathlib.Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


async def measure(engine, text: str) -> tuple[float, float, bytes]:
    t0 = time.monotonic()
    ttfa, chunks = None, []
    async for pcm in engine.stream(text):
        if ttfa is None:
            ttfa = (time.monotonic() - t0) * 1000.0
        chunks.append(pcm)
    total = (time.monotonic() - t0) * 1000.0
    audio = b"".join(chunks)
    audio_ms = len(audio) / 2 / engine.sample_rate * 1000.0
    return ttfa or total, (total / audio_ms if audio_ms else float("nan")), audio


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=sorted(ENGINES), default="mock")
    ap.add_argument("--sentences", type=pathlib.Path, default=ROOT / "bench" / "sentences_ko.txt")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--save-wav", type=pathlib.Path, default=None)
    args = ap.parse_args()

    engine = ENGINES[args.engine]()
    await engine.warmup()
    sentences = [normalize_for_tts(s) for s in load_sentences(args.sentences)]
    ttfas, rtfs = [], []
    for r in range(args.repeat):
        for i, text in enumerate(sentences):
            ttfa, rtf, audio = await measure(engine, text)
            ttfas.append(ttfa)
            rtfs.append(rtf)
            if args.save_wav and r == 0:
                args.save_wav.mkdir(parents=True, exist_ok=True)
                with wave.open(str(args.save_wav / f"{i:02d}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(engine.sample_rate)
                    w.writeframes(audio)
            print(f"[{r}:{i:02d}] TTFA {ttfa:7.1f}ms  RTF {rtf:5.3f}  {text[:30]}")
    print(f"\n{args.engine}: n={len(ttfas)}  "
          f"TTFA P50 {percentile(ttfas, 50):.1f}ms / P90 {percentile(ttfas, 90):.1f}ms  "
          f"RTF P50 {percentile(rtfs, 50):.3f} / P90 {percentile(rtfs, 90):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
