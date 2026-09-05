"""name_optimizer 무드어휘 + content 수요기반 태그 최소 자가 테스트.

python3 bebrave/smartstore/test_name_optimizer.py 로 실행.
네이버 검색광고 API는 monkeypatch로 대체 — 실제 네트워크 호출 없음.
"""
from bebrave.smartstore import content
from bebrave.smartstore.content import MAX_TAGS
from bebrave.smartstore.name_optimizer import optimize_name
from bebrave.sourcing.keyword_tool import KeywordData


class _FakeProduct:
    def __init__(self, name, category):
        self.name = name
        self.category = category


def _fake_related(pairs):
    """[(keyword, monthly_total)] -> KeywordData 리스트 (pc에 전량 몰아넣음, 편의상)."""
    return [KeywordData(keyword=kw, monthly_pc=total, monthly_mobile=0) for kw, total in pairs]


def test_mood_word_added_on_category_match():
    name = optimize_name("큐티클오일", "큐티클오일 100ml", category="생활>뷰티>네일케어")
    assert name.endswith("셀프네일"), name


def test_mood_word_skipped_when_over_max_len():
    long_title = "네일" + "가" * 40  # 이미 45자에 근접/초과
    name = optimize_name("네일", long_title, category="네일케어", max_len=45)
    assert len(name) <= 45
    assert "셀프네일" not in name


def test_demand_tags_relevant_and_ranked(monkeypatch):
    # "마사지"는 원본 제목/카테고리와 겹치는 단어가 없는 무관 키워드라 걸러져야 함
    monkeypatch.setattr(content, "fetch_related_keywords", lambda seed, limit=30: _fake_related([
        ("괄사", 49560), ("다이어트보조제", 158180), ("도자기괄사세트", 210), ("두피마사지기", 0),
    ]))
    product = _FakeProduct("두피 괄사 도자기괄사 목근육 머리마사지기", "건강용품>안마용품")
    tags = content._generate_tags("도자기괄사", product)
    assert tags[0] == "도자기괄사"
    assert "괄사" in tags  # 관련 + 검색량 있음 -> 채택
    assert "다이어트보조제" not in tags  # 원본과 단어 안 겹침 -> 제외
    assert "두피마사지기" not in tags  # 검색량 0 -> 제외
    assert len(tags) <= MAX_TAGS


def test_demand_tags_api_failure_falls_back(monkeypatch):
    def _boom(seed, limit=30):
        raise ValueError("API 키 없음")
    monkeypatch.setattr(content, "fetch_related_keywords", _boom)
    product = _FakeProduct("정리함 대형 수납박스 원룸용", "생활>수납/정리용품>정리함")
    tags = content._generate_tags("정리함", product)
    assert tags[0] == "정리함"
    assert len(tags) <= MAX_TAGS
    assert len(tags) >= 1  # API 실패해도 카테고리/제목 기반으로 최소한은 채워짐


def test_no_regression_when_category_unmatched():
    name = optimize_name("우산", "우산 자동우산 3단자동우산", category="잡화>우산")
    assert name == "우산 자동우산 3단자동우산"


def test_substring_redundant_word_removed():
    # "실리콘주걱"을 이미 골랐는데 뒤에 "실리콘"만 또 나오면 정보가 없으니 제거.
    name = optimize_name("실리콘주걱", "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱", category="주방")
    words = name.split()
    assert words.count("실리콘") == 0, name
    assert "실리콘주걱" in words


def test_compound_word_with_extra_info_kept():
    # "자동우산"은 "우산"의 부분집합이 아니라 정보가 추가된 복합어이므로 유지돼야 한다
    # (substring 필터가 반대 방향으로 오작동해 유용한 복합어까지 지우면 안 됨).
    name = optimize_name("우산", "우산 자동우산 3단자동우산 골프우산", category="잡화>우산")
    assert name == "우산 자동우산 3단자동우산 골프우산", name


def test_blocked_brand_word_removed_from_title():
    name = optimize_name("텀블러", "텀블러 락앤락 보온보냉 500ml", category="주방")
    assert "락앤락" not in name.split(), name


def test_slash_listed_words_are_tokenized():
    # 슬래시로 나열된 통짜 토큰("손톱깍이/손톱깍기/…/인쇄가능")이 공백 split()만으로는
    # 필터를 전부 우회했다(2026-09 발견) — 슬래시도 단어 구분자여야 한다.
    name = optimize_name(
        "손톱깍이세트",
        "손톱깍이세트 손톱깍이/손톱깍기/손톱깍이세트/귀이게/네일/손톱깍기/손톱정리/인쇄가능",
        category="네일케어",
    )
    words = name.split()
    assert "인쇄가능" not in words, name  # 우리가 안 하는 인쇄 서비스 — 허위 표시
    assert "귀이게" in words and "네일" in words  # 정상 단어는 유지


def test_supplier_management_code_removed():
    assert optimize_name("큐티클제거", "고급 큐티클 밀대 2p -TJ/큐티클제거/손톱밀대/네일", category="네일케어") \
        .split().count("-TJ") == 0
    name = optimize_name("파우더퍼프", "파스텔 에어 쿠션 퍼프 메이크업 파운데이션퍼프 1P RD-10098", category="뷰티소품")
    assert "RD-10098" not in name.split(), name


def test_promo_word_substring_blocked():
    # 완전일치였던 예전 판정은 특수문자가 붙으면 통과했다(2026-09 발견).
    name = optimize_name("우산", "무료배송! ★특가★ 3단자동우산", category="잡화>우산")
    assert "무료배송" not in name and "특가" not in name, name


def test_tag_blocklist_applied_to_generated_tags(monkeypatch):
    # 소싱 후보 선별(discover.py)에만 걸리던 차단 목록을 태그 생성에도 적용 —
    # "손톱영양제" 후보가 "손톱무좀" 태그를 달고 있던 사고 재현(2026-09).
    monkeypatch.setattr(content, "fetch_related_keywords", lambda seed, limit=30: [])
    product = _FakeProduct("메니큐어 손톱강화제 손톱무좀 예방 락앤락 케이스", "화장품>네일케어>매니큐어")
    tags = content._generate_tags("손톱강화제", product)
    assert "손톱무좀" not in tags, tags
    assert "락앤락" not in tags, tags


def test_category_name_not_added_as_tag(monkeypatch):
    # 도매매 카테고리명을 그대로 태그로 넣던 코드 제거 확인 — 슬래시 섞인 원문이
    # 태그로 나가면 안 된다.
    monkeypatch.setattr(content, "fetch_related_keywords", lambda seed, limit=30: [])
    product = _FakeProduct("정리함", "생활>수납/정리용품>정리함")
    tags = content._generate_tags("정리함", product)
    assert "수납/정리용품" not in tags and "수납/정리" not in tags, tags


if __name__ == "__main__":
    import inspect

    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]

    class _Monkeypatch:
        def __init__(self):
            self._orig = []

        def setattr(self, obj, attr, value):
            self._orig.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)

        def undo(self):
            for obj, attr, value in self._orig:
                setattr(obj, attr, value)

    for fn in tests:
        mp = _Monkeypatch()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
        finally:
            mp.undo()

    print("OK - all name_optimizer/content self-checks passed")
