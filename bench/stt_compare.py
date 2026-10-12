"""STT 엔진 비교 벤치 (SenseVoice vs Qwen3-ASR).

사용법:
    .venv/bin/python bench/stt_compare.py [wav_dir ...] [--engines sensevoice,qwen3asr] [--limit N]

stt_spacing_bench.py 와 같은 항목(ko.wav + NN.wav)·같은 VAD 분할(pre_roll 300)을 쓴다.
구간 결과를 공백으로 이어 붙여 CER(공백 제외), 간격 오류, 구간별 지연 P50·P90을 낸다.
주의: NN.wav 는 TTS 합성음이다. 실제 사용자 음성이 아니다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "bench")

from stt_spacing_bench import KO_REF, KO_WAV, err_counts, read_16k, vad_segment  # noqa: E402
from tts_roundtrip import load_sentences  # noqa: E402

from voice_pipeline.stt import SenseVoiceSTT  # noqa: E402
from voice_pipeline.stt.qwen3asr import Qwen3ASRSTT  # noqa: E402
from voice_pipeline.trace import percentile  # noqa: E402


def make(engine: str, spacing: bool):
    if engine == "sensevoice":
        return SenseVoiceSTT(spacing=spacing)
    if engine == "qwen3asr":
        return Qwen3ASRSTT(spacing=spacing)
    raise SystemExit(f"알 수 없는 엔진: {engine}")


async def run(a) -> None:
    items = []
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
    segs = [vad_segment(x, 300) for _, x, _ in items]
    print(f"n={len(items)} 문장, 구간 {sum(map(len, segs))}개 (TTS 합성음 포함)")
    print(f"{'engine':<24}{'CER(공백X)':>11}{'CER(공백O)':>11}{'간격오류':>9}{'P50ms':>8}{'P90ms':>8}")
    for eng in a.engines.split(","):
        for spacing in (False, True) if a.both_spacing else (a.spacing,):
            stt = make(eng, spacing)
            await stt.warmup()
            await stt.transcribe(segs[0][0])  # 첫 요청 지연 제외
            e_ns = e_sp = tot = 0
            lat: list[float] = []
            for (name, _, ref), seg in zip(items, segs):
                texts = []
                for g in seg:
                    r = await stt.transcribe(g)
                    texts.append(r.text)
                    lat.append(r.elapsed_ms)
                hyp = " ".join(texts)
                if a.verbose:
                    print(f"  [{name}] {hyp}")
                a1, a2, n = err_counts(ref, hyp)
                e_ns, e_sp, tot = e_ns + a1, e_sp + a2, tot + n
            label = f"{eng}{'+kiwi' if spacing else ''}"
            print(f"{label:<24}{e_ns / tot:>11.3f}{e_sp / tot:>11.3f}{(e_sp - e_ns) / tot:>9.3f}"
                  f"{percentile(lat, 50):>8.0f}{percentile(lat, 90):>8.0f}")
            if hasattr(stt, "close"):
                await stt.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav_dirs", nargs="*", default=["bench/results/voice_calm"])
    ap.add_argument("--sentences", default="bench/sentences_ko.txt")
    ap.add_argument("--engines", default="sensevoice,qwen3asr")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--spacing", action="store_true", help="Kiwi 띄어쓰기 교정 적용")
    ap.add_argument("--both-spacing", action="store_true", help="Kiwi 없음/있음 둘 다")
    ap.add_argument("--verbose", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
