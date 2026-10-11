"""TTS 왕복 명료도 검사: wav -> SenseVoice 전사 -> 원문과 CER 비교.

사용법:
    .venv/bin/python bench/tts_bench.py --engine cosyvoice3 --save-wav bench/results/cosyvoice3
    .venv/bin/python bench/tts_roundtrip.py bench/results/cosyvoice3

NN.wav 는 sentences_ko.txt 의 NN번째 문장(normalize_for_tts 적용 후)에 대응한다.
음절당 길이(초/글자)는 0.1~0.2 가 정상이다. 벗어나면 배속·무한 생성을 의심한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "bench")

from stt_bench import load_wav_16k  # noqa: E402

from voice_pipeline.normalize import normalize_for_tts  # noqa: E402
from voice_pipeline.stt import SenseVoiceSTT  # noqa: E402

PUNCT = ".,?!"


def _chars(s: str) -> list[str]:
    return [c for c in s if c.strip() and c not in PUNCT]


def cer(ref: str, hyp: str) -> float:
    """문자 단위 편집거리 / 원문 길이 (공백·문장부호 제외)."""
    a, b = _chars(ref), _chars(hyp)
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (x != y))
    return d[-1] / max(1, len(a))


def load_sentences(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [normalize_for_tts(s) for l in f if (s := l.strip()) and not s.startswith("#")]


async def main(wav_dir: str, sentences: str) -> None:
    stt = SenseVoiceSTT()
    await stt.warmup()
    cers: list[float] = []
    print(f"{'#':>2} {'dur':>6} {'s/char':>6} {'CER':>5}  원문 -> 전사")
    for i, ref in enumerate(load_sentences(sentences)):
        p = Path(wav_dir) / f"{i:02d}.wav"
        if not p.exists():
            continue
        with wave.open(str(p)) as w:
            dur = w.getnframes() / w.getframerate()
        hyp = (await stt.transcribe(load_wav_16k(str(p)))).text
        c = cer(ref, hyp)
        cers.append(c)
        ratio = dur / max(1, len(_chars(ref)))
        print(f"{i:>2} {dur:5.1f}s {ratio:6.2f} {c:5.2f}  {ref}\n{'':23}-> {hyp}")
    if not cers:
        print("wav 없음:", wav_dir)
        return
    cers.sort()
    n = len(cers)
    print(f"\nn={n}  CER P50={cers[n // 2]:.2f}  P90={cers[min(n - 1, int(n * 0.9))]:.2f}  평균={sum(cers) / n:.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav_dir", help="NN.wav 가 있는 폴더")
    ap.add_argument("--sentences", default="bench/sentences_ko.txt")
    a = ap.parse_args()
    asyncio.run(main(a.wav_dir, a.sentences))
