from voice_pipeline.chunker import SemanticChunker


def run(tokens, **kw):
    c = SemanticChunker(**kw)
    out = []
    for t in tokens:
        out += c.feed(t)
    return out + c.close()


def test_decimal_is_not_split():
    toks = ["오늘", " 측정", "하신", " 체온은", " 36", ".", "5", "도", "입니다", ".", " 정상", " 범위", "예요", "."]
    assert run(toks) == ["오늘 측정하신 체온은 36.5도입니다.", "정상 범위예요."]


def test_integer_followed_by_sentence_end():
    assert run(["혈압은", " 120", ".", " 정상", "입니다", "."]) == ["혈압은 120.", "정상입니다."]


def test_first_chunk_cuts_at_comma_only_once():
    toks = ["오늘", " 측정", "하신", " 결과를", " 보면", ",", " 혈압이", " 조금", ",", " 높게", " 나왔어요", "."]
    assert run(toks) == ["오늘 측정하신 결과를 보면,", "혈압이 조금, 높게 나왔어요."]


def test_forced_split_at_word_boundary():
    out = run(["가나다라마바사 "] * 8)
    assert all(not part.endswith(("가", "나")) or part.endswith("사") for part in out)
    assert all(len(part) <= 40 for part in out)


def test_dot_at_stream_end_is_flushed():
    assert run(["오늘", " 측정하신", " 체온은", " 36", "."]) == ["오늘 측정하신 체온은 36."]


def test_question_mark():
    assert run(["요즘", " 잠은", " 잘", " 주무", "시나요", "?"]) == ["요즘 잠은 잘 주무시나요?"]


def test_sentence_end_inside_a_token_cuts_there():
    """MTP처럼 토큰이 여러 글자를 한 번에 줄 때, 문장 끝이 토큰 중간에 있어도 첫 문장에서 자른다."""
    out = run(["잠이", " 안 오셨구나. 많이", " 힘드셨겠어요."])
    assert out == ["잠이 안 오셨구나.", "많이 힘드셨겠어요."]


def test_two_sentences_in_one_token_with_min_chars():
    # 첫 청크 이후에는 15자 이상이 될 때까지 문장을 모은다
    out = run(["네.", " 천천히 말씀해 주세요. 괜찮아요. 편하게 말씀하세요."], first_min=2)
    assert out == ["네.", "천천히 말씀해 주세요. 괜찮아요.", "편하게 말씀하세요."]


def test_decimal_inside_a_single_token_is_not_split():
    assert run(["체온은 36.5도", "입니다."]) == ["체온은 36.5도입니다."]
