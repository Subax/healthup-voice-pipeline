"""STT 벤치: 디렉터리의 wav를 모두 인식해 경과 ms·RTF·텍스트와 P50·P90을 출력한다.

    .venv/bin/python bench/stt_bench.py [--dir models/sense-voice-int8/test_wavs]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_pipeline.stt import SenseVoiceSTT  # noqa: E402
from voice_pipeline.trace import percentile  # noqa: E402


def load_wav_16k(path: Path) -> bytes:
    """wav를 16 kHz 모노 PCM16 바이트로 읽는다 (필요하면 선형 보간 리샘플)."""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"16bit wav만 지원: {path}")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        n = int(len(x) * 16000 / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="models/sense-voice-int8/test_wavs")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--language", default="ko")
    a = ap.parse_args()
    kw = {"model_dir": a.model_dir} if a.model_dir else {}
    stt = SenseVoiceSTT(num_threads=a.threads, language=a.language, **kw)
    await stt.warmup()
    wavs = sorted(Path(a.dir).glob("*.wav"))
    if not wavs:
        sys.exit(f"wav 없음: {a.dir}")
    times: list[float] = []
    for p in wavs:
        pcm = load_wav_16k(p)
        audio_ms = len(pcm) / 2 / 16.0
        r = await stt.transcribe(pcm)
        times.append(r.elapsed_ms)
        print(f"{p.name:24s} elapsed={r.elapsed_ms:7.1f}ms audio={audio_ms:7.0f}ms "
              f"RTF={r.elapsed_ms / audio_ms:.3f} lang={r.lang} | {r.text}")
    print(f"N={len(times)} P50={percentile(times, 50):.1f}ms P90={percentile(times, 90):.1f}ms "
          f"({stt.name}, threads={a.threads}, CPU)")


if __name__ == "__main__":
    asyncio.run(main())
