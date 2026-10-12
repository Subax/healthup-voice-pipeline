"""STT 띄어쓰기·한자 후처리 비교 벤치.

사용법:
    .venv/bin/python bench/stt_spacing_bench.py [wav_dir ...] [--limit N]

wav_dir 의 NN.wav 는 sentences_ko.txt 의 NN번째 문장(tts_roundtrip과 같은 규칙).
데모와 같이 VADSegmenter 에 320샘플(20 ms) 프레임을 넣어 SpeechEnd pcm 을 전사한다.
간격 오류 = (공백 포함 CER) - (공백 제외 CER).
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "bench")

from tts_roundtrip import PUNCT, load_sentences  # noqa: E402

from voice_pipeline.stt import SenseVoiceSTT, SpeechEnd, VADSegmenter  # noqa: E402
from voice_pipeline.stt.postprocess import fix_spacing, korean_only  # noqa: E402

KO_WAV = Path("models/sense-voice-int8-2025-09/test_wavs/ko.wav")
KO_REF = "조금만 생각을 하면서 살면 훨씬 편할 거야."


def _edit(a: str, b: str) -> int:
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (x != y))
    return d[-1]


def _strip(s: str) -> str:
    return "".join(c for c in s if c not in PUNCT)


def err_counts(ref: str, hyp: str) -> tuple[int, int, int]:
    """(공백 제외 편집, 공백 포함 편집, 원문 글자 수). 공백은 1글자로 센다."""
    r, h = _strip(ref), _strip(hyp)
    r1, h1 = " ".join(r.split()), " ".join(h.split())
    rn, hn = r1.replace(" ", ""), h1.replace(" ", "")
    return _edit(rn, hn), _edit(r1, h1), len(rn)


def read_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        n = int(len(x) * 16000 / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16)


def vad_segment(x: np.ndarray, pre_roll_ms: int) -> list[bytes]:
    """데모와 같은 방식: 320샘플 프레임 + 끝에 무음 1.5초. 모든 구간을 차례로 돌려준다."""
    vad = VADSegmenter(pre_roll_ms=pre_roll_ms)
    x = np.concatenate([np.zeros(3200, np.int16), x, np.zeros(24000, np.int16)])
    segs: list[bytes] = []
    for i in range(0, len(x), 320):
        for ev in vad.feed(x[i : i + 320].tobytes()):
            if isinstance(ev, SpeechEnd):
                segs.append(ev.pcm16 if hasattr(ev, "pcm16") else ev.pcm)
    return segs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav_dirs", nargs="*", default=["bench/results/voice_calm"])
    ap.add_argument("--sentences", default="bench/sentences_ko.txt")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    items: list[tuple[str, np.ndarray, str]] = []
    if KO_WAV.exists():
        items.append(("ko.wav", read_16k(KO_WAV), KO_REF))
    sents = load_sentences(a.sentences)
    for d in a.wav_dirs:
        for i, ref in enumerate(sents):
            p = Path(d) / f"{i:02d}.wav"
            if p.exists():
                items.append((f"{Path(d).name}/{i:02d}", read_16k(p), ref))
    if a.limit:
        items = items[: a.limit]

    raw_stt = SenseVoiceSTT(spacing=False, korean_only=False)
    pad_stt = SenseVoiceSTT(spacing=False, korean_only=False, noise_pad_ms=600)
    raw_stt._load()
    raw_stt._run(bytes(16000))
    fix_spacing("워밍 업")

    # (이름, pre_roll, stt, 한자제거, 띄어쓰기)
    configs = [
        ("whole file (VAD 없음)", 0, raw_stt, False, False),
        ("raw (pre_roll 200)", 200, raw_stt, False, False),
        ("pre_roll 200 +kiwi", 200, raw_stt, True, True),
        ("pre_roll 300", 300, raw_stt, False, False),
        ("+korean_only", 300, raw_stt, True, False),
        ("+kiwi spacing", 300, raw_stt, True, True),
        ("noise_pad 600 (no kiwi)", 300, pad_stt, True, False),
        ("+noise_pad 600 +kiwi", 300, pad_stt, True, True),
    ]
    segs = {r: [vad_segment(x, r) for _, x, _ in items] for r in (200, 300)}
    segs[0] = [[x.tobytes()] for _, x, _ in items]
    han = __import__("re").compile(r"[一-鿿぀-ヿ]")

    print(f"n={len(items)} 문장")
    print(f"{'config':<26}{'CER(공백X)':>11}{'CER(공백O)':>11}{'간격오류':>9}{'한자문장':>9}{'후처리ms':>9}")
    for name, pre, stt, ko, sp in configs:
        e_ns = e_sp = tot = hans = 0
        pp_ms = 0.0
        for (_, _, ref), seg in zip(items, segs[pre]):
            hyp = " ".join(stt._run(g).text for g in seg)  # 구간 결과를 이어 붙인다 (dev-guide §3.3)
            t0 = time.perf_counter()
            if ko:
                hyp = korean_only(hyp)
            if sp:
                hyp = fix_spacing(hyp)
            pp_ms += (time.perf_counter() - t0) * 1000
            hans += bool(han.search(hyp))
            a1, a2, n = err_counts(ref, hyp)
            e_ns, e_sp, tot = e_ns + a1, e_sp + a2, tot + n
        print(
            f"{name:<26}{e_ns / tot:>11.3f}{e_sp / tot:>11.3f}{(e_sp - e_ns) / tot:>9.3f}"
            f"{hans:>9}{pp_ms / len(items):>9.2f}"
        )


if __name__ == "__main__":
    main()
