"""D1 음성 명령 녹음 평가 단위 시험 (tests/test_intent_eval_wav.py).

vLLM 없이 mock STT를 사용하여:
1. 녹음 대본 TSV 파싱 (5열 규격)
2. 누락된 WAV 파일 보고 및 건너뛰기
3. mock STT 기반 음성 인식 -> 의도 분류 파이프라인
4. 혼동표(Confusion Matrix), 명령 재현율, 되돌릴 수 없는 명령 오실행 집계
5. 결과 파일(intent_wav_<날짜>.txt) 저장 및 CLI 연동
을 검증한다.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import wave

import numpy as np
import pytest

# voice_pipeline 및 bench 경로 추가
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from bench.intent_eval import (
    WavTestCase,
    load_wav_pcm16,
    load_wav_script,
    run_wav_evaluation,
)
from voice_pipeline.web.mocks import MockSTT, MockSTTResult


def _create_dummy_wav(path: Path, duration_s: float = 0.2, sample_rate: int = 16000) -> None:
    """테스트용 초단기 16kHz 모노 16bit PCM WAV 생성."""
    n_samples = int(duration_s * sample_rate)
    samples = (np.sin(2 * np.pi * 440 * np.linspace(0, duration_s, n_samples)) * 10000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())


class MappingMockSTT:
    """PCM 바이트 길이나 호출 순서에 따라 정해진 텍스트를 반환하는 테스트용 STT."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.call_count = 0

    def _run(self, pcm16: bytes) -> MockSTTResult:
        if self.call_count < len(self.responses):
            text = self.responses[self.call_count]
        else:
            text = ""
        self.call_count += 1
        return MockSTTResult(text=text, elapsed_ms=1.5)


def test_load_wav_script_full():
    """bench/intent_wav_script_ko.tsv 파싱 200문장 무결성 검증."""
    script_path = REPO_ROOT / "bench" / "intent_wav_script_ko.tsv"
    assert script_path.exists(), f"대본 파일이 없습니다: {script_path}"

    cases = load_wav_script(script_path)
    assert len(cases) == 200, f"대본 항목 수는 200개여야 함: {len(cases)}"

    # 1. 첫 항목 (SESSION_START, idle)
    c0 = cases[0]
    assert c0.text == "시작하자"
    assert c0.expected_intent == "SESSION_START"
    assert c0.wav_filename == "001_SESSION_START_1_r1.wav"
    assert c0.repeat == 1
    assert c0.eval_state == "idle"

    # 2. 정지 명령 (STOP_SPEAKING, speaking)
    c10 = cases[10]
    assert c10.expected_intent == "STOP_SPEAKING"
    assert c10.eval_state == "speaking"

    # 3. 비명령 대화문 (CHAT, active)
    c170 = cases[170]
    assert c170.expected_intent == "CHAT"
    assert c170.wav_filename == "171_CHAT_01.wav"

    # 4. 위기 표현 (CRISIS, active)
    c190 = cases[190]
    assert c190.expected_intent == "CRISIS"
    assert c190.wav_filename == "191_CRISIS_01.wav"


def test_wav_eval_missing_wavs(tmp_path: Path):
    """지정 폴더에 WAV가 하나도 없을 때 누락 목록을 보고하고 안전하게 종료하는지 검증."""
    script_path = REPO_ROOT / "bench" / "intent_wav_script_ko.tsv"
    empty_wav_dir = tmp_path / "empty_wavs"
    empty_wav_dir.mkdir()
    out_file = tmp_path / "result_missing.txt"

    all_passed, summary = run_wav_evaluation(
        script_path=script_path,
        wav_dir=empty_wav_dir,
        output_path=out_file,
    )

    assert not all_passed
    assert summary["found_count"] == 0
    assert summary["missing_count"] == 200
    assert out_file.exists()

    content = out_file.read_text(encoding="utf-8")
    assert "누락된 WAV 파일: 200개" in content
    assert "001_SESSION_START_1_r1.wav" in content
    assert "FAIL" in content


def test_wav_eval_with_mock_stt(tmp_path: Path):
    """mock STT로 실제 WAV를 읽어 의도 분류, 혼동표, 재현율이 정상 계산되는지 검증."""
    # 미니 대본 작성 (명령 2개 + CHAT 1개 + 누락 1개)
    mini_script = tmp_path / "mini_script.tsv"
    mini_script.write_text(
        "시작하자\tSESSION_START\t001_start.wav\t1\tidle\n"
        "그만\tSTOP_SPEAKING\t002_stop.wav\t1\tspeaking\n"
        "오늘 날씨가 춥네요\tCHAT\t003_chat.wav\t1\tactive\n"
        "다 끝내고 싶어\tCRISIS\t004_missing.wav\t1\tactive\n",
        encoding="utf-8",
    )

    wav_dir = tmp_path / "wavs"
    wav_dir.mkdir()
    _create_dummy_wav(wav_dir / "001_start.wav")
    _create_dummy_wav(wav_dir / "002_stop.wav")
    _create_dummy_wav(wav_dir / "003_chat.wav")
    # 004_missing.wav 는 생성하지 않음

    # STT가 정확한 텍스트를 반환하도록 모의
    mock_stt = MappingMockSTT(["시작하자", "그만", "오늘 날씨가 춥네요"])
    out_file = tmp_path / "mini_result.txt"

    all_passed, summary = run_wav_evaluation(
        script_path=mini_script,
        wav_dir=wav_dir,
        stt_instance=mock_stt,
        output_path=out_file,
    )

    assert all_passed, f"평가 통과 실패: {summary}"
    assert summary["found_count"] == 3
    assert summary["missing_count"] == 1
    assert summary["cmd_total"] == 2
    assert summary["cmd_correct"] == 2
    assert summary["cmd_recall"] == 100.0
    assert summary["irreversible_fps"] == 0
    assert summary["crisis_fps"] == 0

    content = out_file.read_text(encoding="utf-8")
    assert "명령 재현율: 2/2 (100.0%)" in content
    assert "되돌릴 수 없는/법적의무 명령 오실행: 0건" in content
    assert "혼동표 (Confusion Matrix)" in content
    assert "SESSION_START" in content
    assert "STOP_SPEAKING" in content
    assert "004_missing.wav" in content
    assert "ALL PASS" in content


def test_wav_eval_detects_irreversible_fp(tmp_path: Path):
    """비명령(CHAT)이 되돌릴 수 없는 명령(RESET 등)으로 오인식되었을 때 치명적 오실행으로 감지하는지 검증."""
    mini_script = tmp_path / "mini_script_fp.tsv"
    mini_script.write_text(
        "날씨 이야기\tCHAT\tchat.wav\t1\tactive\n",
        encoding="utf-8",
    )
    wav_dir = tmp_path / "wavs_fp"
    wav_dir.mkdir()
    _create_dummy_wav(wav_dir / "chat.wav")

    # STT가 CHAT 음성을 "처음부터 다시" (RESET)로 오인식했다고 가정
    mock_stt = MappingMockSTT(["처음부터 다시"])
    out_file = tmp_path / "result_fp.txt"

    all_passed, summary = run_wav_evaluation(
        script_path=mini_script,
        wav_dir=wav_dir,
        stt_instance=mock_stt,
        output_path=out_file,
    )

    assert not all_passed
    assert summary["irreversible_fps"] == 1
    assert not summary["pass_irreversible"]

    content = out_file.read_text(encoding="utf-8")
    assert "치명적 오실행" in content
    assert "RESET" in content


def test_wav_eval_detects_crisis_fp(tmp_path: Path):
    """위기 표현(CRISIS)이 일반 명령(STOP_SPEAKING)으로 오인식되었을 때 위기 오판으로 감지하는지 검증."""
    mini_script = tmp_path / "mini_script_crisis.tsv"
    mini_script.write_text(
        "다 끝내고 싶어\tCRISIS\tcrisis.wav\t1\tactive\n",
        encoding="utf-8",
    )
    wav_dir = tmp_path / "wavs_crisis"
    wav_dir.mkdir()
    _create_dummy_wav(wav_dir / "crisis.wav")

    # STT가 "대화 끝낼게요" (SESSION_END)로 오인식
    mock_stt = MappingMockSTT(["대화 끝낼게요"])
    out_file = tmp_path / "result_crisis.txt"

    all_passed, summary = run_wav_evaluation(
        script_path=mini_script,
        wav_dir=wav_dir,
        stt_instance=mock_stt,
        output_path=out_file,
    )

    assert not all_passed
    assert summary["crisis_fps"] == 1
    assert not summary["pass_crisis"]

    content = out_file.read_text(encoding="utf-8")
    assert "위기 오판" in content


def test_cli_wav_dir_option(tmp_path: Path):
    """CLI 옵션 --wav-dir --stt mock --output 정상 구동 검증."""
    mini_script = tmp_path / "cli_script.tsv"
    mini_script.write_text(
        "시작하자\tSESSION_START\t001.wav\t1\tidle\n",
        encoding="utf-8",
    )
    wav_dir = tmp_path / "cli_wavs"
    wav_dir.mkdir()
    _create_dummy_wav(wav_dir / "001.wav")
    out_file = tmp_path / "cli_out.txt"

    cmd = [
        sys.executable,
        str(REPO_ROOT / "bench" / "intent_eval.py"),
        "--wav-dir", str(wav_dir),
        "--script", str(mini_script),
        "--stt", "mock",
        "--output", str(out_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    # mock STT 기본 반환은 "안녕하세요, 오늘 혈압 결과 알려주세요." -> TALK 이므로 SESSION_START 기대와 달라 FAIL(1) 정상
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "HealthUp 음성 명령 녹음 평가 보고서" in content
