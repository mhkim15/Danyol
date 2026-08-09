"""최소 자가검증 — 프레임워크 없이 python3 -m bebrave.sourcing.test_analyzer 로 실행."""
from .analyzer import dedupe_by_supply
from .models import ProductCandidate


def _cand(keyword, score, goods_no="", supply_name=""):
    return ProductCandidate(
        keyword=keyword, monthly_search=0, product_count=0, category="테스트",
        score=score, supply_goods_no=goods_no, supply_name=supply_name,
    )


def test():
    # 같은 상품코드로 매칭된 동의어 키워드는 점수 높은 것만 남는다
    # ("빨래세제"/"세탁세제"가 같은 도매매 상품에 매칭됐던 사례, 2026-08).
    same_code = [
        _cand("세탁세제", 55, goods_no="G1"),
        _cand("빨래세제", 62, goods_no="G1"),
    ]
    kept, removed = dedupe_by_supply(same_code)
    assert [c.keyword for c in kept] == ["빨래세제"]
    assert [c.keyword for c in removed] == ["세탁세제"]

    # 상품코드가 없으면 상품명 완전일치로 대체 판단.
    same_name = [
        _cand("A", 40, supply_name="동일 상품명"),
        _cand("B", 70, supply_name="동일 상품명"),
    ]
    kept, removed = dedupe_by_supply(same_name)
    assert [c.keyword for c in kept] == ["B"]

    # 매칭 자체가 안 된(코드도 이름도 없는) 항목은 중복판단 대상이 아니라 그대로 남는다.
    unmatched = [_cand("C", 30), _cand("D", 30)]
    kept, removed = dedupe_by_supply(unmatched)
    assert len(kept) == 2 and len(removed) == 0

    # 서로 다른 상품은 당연히 둘 다 남는다.
    distinct = [_cand("E", 50, goods_no="G2"), _cand("F", 50, goods_no="G3")]
    kept, removed = dedupe_by_supply(distinct)
    assert len(kept) == 2 and len(removed) == 0

    print("ok")


if __name__ == "__main__":
    test()
