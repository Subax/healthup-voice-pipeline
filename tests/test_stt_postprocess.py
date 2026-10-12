"""STT 후처리 테스트."""

from __future__ import annotations

import pytest

from voice_pipeline.stt.postprocess import fix_spacing, korean_only


def test_korean_only_removes_han_and_kana():
    assert korean_only("嗯。네，") == "네,"
    assert korean_only("嗯。") == ""
    assert korean_only("あ안녕 漢字 하세요！") == "안녕 하세요!"


def test_korean_only_keeps_hangul_digits_latin():
    assert korean_only("BMI 는  23 입니다？") == "BMI 는 23 입니다?"


def test_fix_spacing_syllable_split():
    pytest.importorskip("kiwipiepy")
    out = fix_spacing("조 금만 생각 을 하 면서 살 면 훨씬 편할 거야.")
    assert out.startswith("조금만 생각을 하면서 살면")


def test_fix_spacing_empty():
    assert fix_spacing("") == ""
