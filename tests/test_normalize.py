import pytest

from voice_pipeline.normalize import normalize_for_tts


@pytest.mark.parametrize("src,expected", [
    ("혈압은 120/80mmHg입니다.", "혈압은 120에 80입니다."),
    ("혈당은 126mg/dL이에요.", "혈당은 126 밀리그램 퍼 데시리터이에요."),
    ("체온은 36.5℃예요.", "체온은 36.5도예요."),
    ("체지방률은 25.3%입니다.", "체지방률은 25.3 퍼센트입니다."),
    ("하루 3~4회 걸으세요.", "하루 3에서 4회 걸으세요."),
    ("**주의** 물을 드세요 😊", "주의 물을 드세요"),
    ("- 첫째, 휴식", "첫째, 휴식"),
    ("수면(7시간 이상)이 중요해요.", "수면, 7시간 이상, 이 중요해요."),
])
def test_normalize(src, expected):
    assert normalize_for_tts(src) == expected


def test_decimal_untouched():
    assert normalize_for_tts("BMI는 23.4예요.") == "BMI는 23.4예요."
