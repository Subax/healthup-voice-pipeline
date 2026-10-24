"""D2 지연 회귀 집계기 단위 시험."""

from pathlib import Path
import pytest
import sys

# bench 경로 추가
bench_dir = Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(bench_dir))

from d2_summarize import (
    LatencyRow,
    parse_lat_text,
    summarize_rows,
    evaluate_d2,
    parse_lat_files,
)


SAMPLE_LAT_TEXT = """
LOG: [판단] 턴 확정 — 종결 어미 '-어요' · Smart Turn 0.98 → 200ms 대기 · ST 0.98 · "테스트 문장 1"
#1 테스트 문장 1
LAT: #4\t36\t280\t540\t740\t280\t1720\t340
#3\t30\t260\t490\t790\t260\t1860\t320
#2\t45\t290\t500\t710\t290\t1710\t350
#1\t67\t300\t480\t790\t300\t1800\t380
ERR: 
BADGE: 듣는 중
"""


def test_parse_and_evaluate_d2():
    # 1. 텍스트 파싱 검증
    rows = parse_lat_text(SAMPLE_LAT_TEXT)
    assert len(rows) == 4
    assert rows[0].turn == "#4"
    assert rows[0].turn_confirmed == 280.0
    assert rows[0].tts_first_audio == 1720.0
    assert rows[3].turn == "#1"
    assert rows[3].turn_confirmed == 300.0
    assert rows[3].tts_first_audio == 1800.0

    # 2. 통계 요약 검증
    summary = summarize_rows(rows, name="TestMode")
    assert summary.n_turns == 4
    # confirmed sorted: [260, 280, 290, 300] -> p50 = 285.0
    assert summary.confirmed_p50 == pytest.approx(285.0)
    # audio sorted: [1710, 1720, 1800, 1860] -> p50 = 1760.0
    assert summary.first_audio_p50 == pytest.approx(1760.0)

    # 3. 합격 기준 평가 검증
    # Mode B 생성 (약간 느리거나 비슷한 조건)
    rows_b = [
        LatencyRow("#4", 35, 275, 530, 730, 275, 1750, 340),
        LatencyRow("#3", 30, 255, 480, 780, 255, 1850, 320),
        LatencyRow("#2", 45, 285, 490, 700, 285, 1700, 350),
        LatencyRow("#1", 65, 295, 470, 780, 295, 1790, 380),
    ]
    # B: confirmed p50 = 280.0, audio p50 = 1770.0
    # diff confirmed: 285 - 280 = +5.0 (<= +20) -> PASS
    # diff audio: 1760 - 1770 = -10.0 (<= +50) -> PASS
    res = evaluate_d2(rows, rows_b)
    assert res.passed is True
    assert res.pass_confirmed is True
    assert res.pass_first_audio is True
    assert res.diff_confirmed == pytest.approx(5.0)
    assert res.diff_first_audio == pytest.approx(-10.0)

    # 4. 불합격 기준 평가 검증 (오디오 차이 > +50ms)
    rows_slow_a = [
        LatencyRow("#1", 30, 350, 500, 700, 300, 2000, 300),
    ]
    rows_fast_b = [
        LatencyRow("#1", 30, 250, 500, 700, 300, 1700, 300),
    ]
    res_fail = evaluate_d2(rows_slow_a, rows_fast_b)
    assert res_fail.passed is False
    assert res_fail.pass_confirmed is False   # diff 100 > 20
    assert res_fail.pass_first_audio is False  # diff 300 > 50


def test_d2_summarize_existing_files():
    # 기존 e2e_on1.txt 및 e2e_off1.txt 실측 파일 검증
    p_on1 = Path("bench/results/latency_2610/e2e_on1.txt")
    p_off1 = Path("bench/results/latency_2610/e2e_off1.txt")
    if not (p_on1.exists() and p_off1.exists()):
        pytest.skip("latency_2610 e2e files not found")

    rows_on = parse_lat_files([p_on1])
    rows_off = parse_lat_files([p_off1])

    assert len(rows_on) == 4
    assert len(rows_off) == 4

    res = evaluate_d2(rows_on, rows_off)
    assert res.passed is True
    assert res.diff_confirmed <= 20.0
    assert res.diff_first_audio <= 50.0
