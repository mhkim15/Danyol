"""name_optimizer 무드어휘 + content 수요기반 태그 최소 자가 테스트.

python3 bebrave/smartstore/test_name_optimizer.py 로 실행.
네이버 검색광고 API는 monkeypatch로 대체 — 실제 네트워크 호출 없음.
"""
from bebrave.smartstore import content
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
    assert len(tags) <= 5


def test_demand_tags_api_failure_falls_back(monkeypatch):
    def _boom(seed, limit=30):
        raise ValueError("API 키 없음")
    monkeypatch.setattr(content, "fetch_related_keywords", _boom)
    product = _FakeProduct("정리함 대형 수납박스 원룸용", "생활>수납/정리용품>정리함")
    tags = content._generate_tags("정리함", product)
    assert tags[0] == "정리함"
    assert len(tags) <= 5
    assert len(tags) >= 1  # API 실패해도 카테고리/제목 기반으로 최소한은 채워짐


def test_no_regression_when_category_unmatched():
    name = optimize_name("우산", "우산 자동우산 3단자동우산", category="잡화>우산")
    assert name == "우산 자동우산 3단자동우산"


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
