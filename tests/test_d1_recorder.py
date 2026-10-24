"""D1 녹음기 단위 시험 (tests/test_d1_recorder.py).

검증 항목:
    1. 저장 파일명 검증 및 경로 탈출(path traversal) 차단
    2. 16kHz 모노 16bit WAV 형식 유효성 검증 (채널·샘플레이트·비트)
    3. aiohttp TestClient 기반 API 흐름 (목록 조회, 파일 업로드, 다시 듣기 오디오 서빙, 삭제)
    4. HTTP API 수준에서의 경로 탈출 및 비정상 WAV 파일 업로드 차단 (400 Bad Request)
"""

from __future__ import annotations

import io
from pathlib import Path
import sys
import wave

# 저장소 루트 sys.path 추가 (bench 모듈 임포트용)
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aiohttp.test_utils import TestClient, TestServer
import numpy as np
import pytest

from bench.d1_recorder import (
    create_app,
    validate_wav_bytes,
    validate_wav_filename,
)


def _make_pcm16_wav(
    channels: int = 1,
    sample_rate: int = 16000,
    sampwidth: int = 2,
    duration_s: float = 0.2,
) -> bytes:
    """테스트용 WAV 바이트 생성 헬퍼."""
    n_samples = int(duration_s * sample_rate)
    if sampwidth == 2:
        dtype = np.int16
        data = (np.sin(2 * np.pi * 440 * np.linspace(0, duration_s, n_samples * channels)) * 10000).astype(dtype)
    elif sampwidth == 1:
        dtype = np.uint8
        data = ((np.sin(2 * np.pi * 440 * np.linspace(0, duration_s, n_samples * channels)) + 1.0) * 127).astype(dtype)
    elif sampwidth == 4:
        dtype = np.int32
        data = (np.sin(2 * np.pi * 440 * np.linspace(0, duration_s, n_samples * channels)) * 1000000).astype(dtype)
    else:
        raise ValueError(f"지원하지 않는 sampwidth: {sampwidth}")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())
    return buf.getvalue()


def test_validate_wav_filename_valid():
    """정상적인 WAV 파일명 검증 통과."""
    clean = validate_wav_filename("001_SESSION_START_1_r1.wav")
    assert clean == "001_SESSION_START_1_r1.wav"

    allowed = {"001_SESSION_START_1_r1.wav", "002_SESSION_START_1_r2.wav"}
    clean_allowed = validate_wav_filename("001_SESSION_START_1_r1.wav", allowed)
    assert clean_allowed == "001_SESSION_START_1_r1.wav"

    with pytest.raises(ValueError, match="대본에 정의되지 않은"):
        validate_wav_filename("999_UNKNOWN.wav", allowed)


def test_validate_wav_filename_path_traversal():
    """상위 디렉터리 접근(..) 및 절대/상대 경로 구분자 탈출 시도 차단."""
    dangerous = [
        "../secret.wav",
        "..\\secret.wav",
        "/etc/passwd.wav",
        "nested/001.wav",
        "001.wav/../../evil.wav",
        "001.txt",
        "",
        "\x00hack.wav",
    ]
    for fn in dangerous:
        with pytest.raises(ValueError):
            validate_wav_filename(fn)


def test_validate_wav_bytes_rules():
    """16kHz 모노 16bit Linear PCM 규격 일치 여부 검증."""
    # 1. 정상: 1ch, 16000Hz, 16bit(sw=2)
    valid_wav = _make_pcm16_wav(channels=1, sample_rate=16000, sampwidth=2)
    ch, sw, sr = validate_wav_bytes(valid_wav)
    assert ch == 1
    assert sw == 2
    assert sr == 16000

    # 2. 비정상: 스테레오(2채널) 거부
    stereo_wav = _make_pcm16_wav(channels=2, sample_rate=16000, sampwidth=2)
    with pytest.raises(ValueError, match="모노"):
        validate_wav_bytes(stereo_wav)

    # 3. 비정상: 샘플레이트(44100Hz) 거부
    sr44_wav = _make_pcm16_wav(channels=1, sample_rate=44100, sampwidth=2)
    with pytest.raises(ValueError, match="16,000 Hz"):
        validate_wav_bytes(sr44_wav)

    # 4. 비정상: 8bit 거부
    sw1_wav = _make_pcm16_wav(channels=1, sample_rate=16000, sampwidth=1)
    with pytest.raises(ValueError, match="16비트 PCM"):
        validate_wav_bytes(sw1_wav)

    # 5. 비정상: 헤더 크기 부족
    with pytest.raises(ValueError, match="헤더 크기 부족"):
        validate_wav_bytes(b"RIFFshort")

    # 6. 비정상: 44바이트 이상이지만 깨진 헤더
    with pytest.raises(ValueError, match="올바른 WAV 형식"):
        validate_wav_bytes(b"RIFF" + b"X" * 50)


@pytest.mark.asyncio
async def test_recorder_api_flow(tmp_path: Path):
    """녹음기 API 전체 흐름 (HTML 제공, 항목 목록 조회, WAV 업로드, 재생, 삭제)."""
    mini_tsv = tmp_path / "script.tsv"
    mini_tsv.write_text(
        "시작하자\tSESSION_START\t001_SESSION_START_1_r1.wav\t1\tidle\n"
        "그만\tSTOP_SPEAKING\t011_STOP_SPEAKING_1_r1.wav\t1\tspeaking\n",
        encoding="utf-8",
    )
    wav_dir = tmp_path / "wav_output"
    wav_dir.mkdir()

    app = create_app(mini_tsv, wav_dir)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. 메인 HTML 서빙 확인
        resp_index = await client.get("/")
        assert resp_index.status == 200
        html = await resp_index.text()
        assert "HealthUp D1 음성 명령 녹음기" in html

        # 2. 목록 조회 (모두 exists: False)
        resp_items = await client.get("/api/items")
        assert resp_items.status == 200
        items = await resp_items.json()
        assert len(items) == 2
        assert items[0]["filename"] == "001_SESSION_START_1_r1.wav"
        assert items[0]["exists"] is False
        assert items[1]["exists"] is False

        # 3. WAV 파일 업로드
        valid_wav = _make_pcm16_wav(channels=1, sample_rate=16000, sampwidth=2)
        resp_up = await client.post(
            "/api/upload?filename=001_SESSION_START_1_r1.wav",
            data=valid_wav,
            headers={"Content-Type": "audio/wav"},
        )
        assert resp_up.status == 200
        up_data = await resp_up.json()
        assert up_data["ok"] is True
        assert (wav_dir / "001_SESSION_START_1_r1.wav").is_file()

        # 4. 목록 재조회 (001은 exists: True)
        resp_items2 = await client.get("/api/items")
        items2 = await resp_items2.json()
        assert items2[0]["exists"] is True
        assert items2[1]["exists"] is False

        # 5. 오디오 서빙 (재생)
        resp_audio = await client.get("/api/audio/001_SESSION_START_1_r1.wav")
        assert resp_audio.status == 200
        audio_bytes = await resp_audio.read()
        assert len(audio_bytes) == len(valid_wav)

        # 6. 파일 삭제 (재녹음 대비)
        resp_del = await client.delete("/api/audio/001_SESSION_START_1_r1.wav")
        assert resp_del.status == 200
        del_data = await resp_del.json()
        assert del_data["ok"] is True
        assert not (wav_dir / "001_SESSION_START_1_r1.wav").exists()

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_recorder_api_security_path_traversal(tmp_path: Path):
    """HTTP 요청 파라미터 수준에서의 경로 탈출 및 형식 위반 차단 검증 (400 Bad Request)."""
    mini_tsv = tmp_path / "script.tsv"
    mini_tsv.write_text("시작하자\tSESSION_START\t001_SESSION_START_1_r1.wav\t1\tidle\n", encoding="utf-8")
    wav_dir = tmp_path / "wav_output"
    wav_dir.mkdir()

    app = create_app(mini_tsv, wav_dir)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        valid_wav = _make_pcm16_wav(channels=1, sample_rate=16000, sampwidth=2)

        # 1. ../ 경로 탈출 파라미터 업로드 시도 -> 400
        resp1 = await client.post("/api/upload?filename=../hack.wav", data=valid_wav)
        assert resp1.status == 400

        # 2. 대본에 없는 임의 파일명 업로드 시도 -> 400
        resp2 = await client.post("/api/upload?filename=unregistered.wav", data=valid_wav)
        assert resp2.status == 400

        # 3. 비정상(44.1kHz) WAV 파일 업로드 시도 -> 400
        invalid_sr = _make_pcm16_wav(channels=1, sample_rate=44100, sampwidth=2)
        resp3 = await client.post(
            "/api/upload?filename=001_SESSION_START_1_r1.wav",
            data=invalid_sr,
        )
        assert resp3.status == 400

        # 4. ../ 경로 탈출 오디오 다운로드 시도 -> 400
        resp4 = await client.get("/api/audio?filename=../secret.wav")
        assert resp4.status == 400

        # 5. ../ 경로 탈출 삭제 시도 -> 400
        resp5 = await client.delete("/api/audio?filename=../secret.wav")
        assert resp5.status == 400

    finally:
        await client.close()


def test_index_html_safari_fixes():
    """Safari 대응: AudioContext resume, 업로드 재시도, 저장 후 자동 이동 및 레이스 컨디션 방지."""
    from bench.d1_recorder import INDEX_HTML

    assert "audioCtx.resume()" in INDEX_HTML
    assert "uploadWithRetry" in INDEX_HTML
    assert "currentIndex === savedIndex" in INDEX_HTML
    assert "getAudioContext()" in INDEX_HTML
    assert "isBusy" in INDEX_HTML
    assert "isStarting" in INDEX_HTML
    assert "e.repeat" in INDEX_HTML
    assert "body.slice(0)" in INDEX_HTML


@pytest.mark.asyncio
async def test_recorder_cors_and_preflight(tmp_path: Path):
    """Safari CORS 대응: OPTIONS 프리플라이트 및 CORS/Keep-Alive 헤더 검증."""
    mini_tsv = tmp_path / "script.tsv"
    mini_tsv.write_text("시작하자\tSESSION_START\t001_SESSION_START_1_r1.wav\t1\tidle\n", encoding="utf-8")
    wav_dir = tmp_path / "wav_output"
    wav_dir.mkdir()

    app = create_app(mini_tsv, wav_dir)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. /api/upload OPTIONS 프리플라이트 요청 -> 204 및 CORS 헤더
        resp_opt = await client.options("/api/upload")
        assert resp_opt.status == 204
        assert resp_opt.headers.get("Access-Control-Allow-Origin") == "*"
        assert "POST" in resp_opt.headers.get("Access-Control-Allow-Methods", "")
        assert "Content-Type" in resp_opt.headers.get("Access-Control-Allow-Headers", "")

        # 2. /api/items OPTIONS 프리플라이트 요청 -> 204 및 CORS 헤더
        resp_opt_items = await client.options("/api/items")
        assert resp_opt_items.status == 204
        assert resp_opt_items.headers.get("Access-Control-Allow-Origin") == "*"

        # 3. /api/upload POST 요청 응답에 CORS 및 Keep-Alive 헤더 포함 확인
        valid_wav = _make_pcm16_wav(channels=1, sample_rate=16000, sampwidth=2)
        resp_up = await client.post(
            "/api/upload?filename=001_SESSION_START_1_r1.wav",
            data=valid_wav,
            headers={"Content-Type": "audio/wav"},
        )
        assert resp_up.status == 200
        assert resp_up.headers.get("Access-Control-Allow-Origin") == "*"
        assert "timeout=120" in resp_up.headers.get("Keep-Alive", "")

        # 4. 400 Bad Request 에러 응답에도 CORS 헤더가 유지되는지 확인 (Safari 에러 차단 방지)
        resp_err = await client.post("/api/upload?filename=invalid.wav", data=b"bad")
        assert resp_err.status == 400
        assert resp_err.headers.get("Access-Control-Allow-Origin") == "*"

    finally:
        await client.close()
