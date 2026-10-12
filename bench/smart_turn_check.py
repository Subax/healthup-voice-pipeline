"""Smart Turn v3.2 점검: 완전한 문장 vs 중간에서 자른 문장의 완료 확률, CPU 지연.

실행: .venv/bin/python bench/smart_turn_check.py [--n 15] [--threads 2]
"""

from __future__ import annotations

import argparse
import statistics as st
import time
import wave
from pathlib import Path

import numpy as np

from voice_pipeline.endpoint.smart_turn import SmartTurnScorer

ROOT = Path(__file__).resolve().parent.parent
SENSE_WAV = ROOT / "models/sense-voice-int8-2025-09/test_wavs/ko.wav"
TTS_DIR = ROOT / "bench/results/voice_calm"
SILENCE_MS = 200


def read_wav16k(path: Path) -> np.ndarray:
    """wav -> 16kHz 모노 float32 (numpy 보간으로 리샘플)."""
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    if sr != 16000:
        n = int(len(x) * 16000 / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def speech_span(x: np.ndarray, thr: float = 0.01, frame: int = 320) -> tuple[int, int]:
    """프레임 RMS가 문턱을 넘는 첫·마지막 위치 (VAD 대용)."""
    n = len(x) // frame
    rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(1))
    idx = np.where(rms > thr)[0]
    if len(idx) == 0:
        return 0, len(x)
    return int(idx[0] * frame), int((idx[-1] + 1) * frame)


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--threads", type=int, default=2)
    a = ap.parse_args()
    scorer = SmartTurnScorer(num_threads=a.threads)
    scorer.warmup()
    sil = np.zeros(16000 * SILENCE_MS // 1000, dtype=np.float32)

    items = [("ko.wav", read_wav16k(SENSE_WAV))]
    for i, p in enumerate(sorted(TTS_DIR.glob("[0-9][0-9].wav"))[: a.n]):
        items.append((p.name, read_wav16k(p)))

    rows, times = [], []
    for name, x in items:
        s, e = speech_span(x)
        sp = x[s:e]
        t0 = time.perf_counter()
        full = scorer.score(pcm(np.concatenate([sp, sil])))
        times.append((time.perf_counter() - t0) * 1000)
        c = []
        for frac in (0.5, 0.7):
            t0 = time.perf_counter()
            c.append(scorer.score(pcm(np.concatenate([sp[: int(len(sp) * frac)], sil]))))
            times.append((time.perf_counter() - t0) * 1000)
        rows.append((name, len(sp) / 16000, full, c[0], c[1]))

    print(f"{'파일':8} {'발화(s)':>7} {'full':>6} {'cut50':>6} {'cut70':>6}")
    for r in rows:
        print(f"{r[0]:8} {r[1]:7.2f} {r[2]:6.3f} {r[3]:6.3f} {r[4]:6.3f}")
    # ko.wav는 TTS가 아니라 실제 녹음이라 요약은 TTS만으로 따로 낸다
    for label, rs in (("전체", rows), ("TTS만", rows[1:])):
        full = [r[2] for r in rs]
        cuts = [v for r in rs for v in (r[3], r[4])]
        print(
            f"[{label}] full 평균 {st.mean(full):.3f} 중앙 {st.median(full):.3f} | "
            f"cut 평균 {st.mean(cuts):.3f} 중앙 {st.median(cuts):.3f} | "
            f"full>0.5: {sum(v > 0.5 for v in full)}/{len(full)} | cut<0.5: {sum(v < 0.5 for v in cuts)}/{len(cuts)}"
        )
    t = sorted(times)
    print(f"CPU {a.threads}스레드, warmup 후 {len(t)}회: P50 {t[len(t)//2]:.1f}ms P90 {t[int(len(t)*0.9)]:.1f}ms (특징 추출 포함)")


if __name__ == "__main__":
    main()
