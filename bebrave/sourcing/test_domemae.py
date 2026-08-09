"""최소 자가검증 — 프레임워크 없이 python3 -m bebrave.sourcing.test_domemae 로 실행."""
from .domemae import _form_signals, _tokenize, find_matching_product, DomemaeProduct


def _p(name, price=1000):
    return DomemaeProduct(
        goods_no="G1", name=name, supply_price=price, retail_price=0,
        min_order_qty=1, stock=0, supplier="", category="",
    )


def test():
    # "~기"로 끝나는 무관 단어가 더 이상 형태신호로 오판정되지 않아야 한다
    # ("팔찌만들기"가 "우레탄 줄"과 매칭됐던 버그, 2026-08).
    assert _form_signals(_tokenize("팔찌만들기")) == set()
    assert _form_signals(_tokenize("화장실변기청소")) == set()
    # 진짜 기기류는 여전히 못 잡는다는 한계는 있지만(예: "샤워기"), 이건
    # "확신 없으면 불확실 처리"라는 안전한 기본값으로 흡수된다 — 새 항목을
    # 추가해 넓히는 건 오탐 사례가 더 쌓이면 그때.
    assert _form_signals(_tokenize("발각질제거기")) == {"제거기"}
    assert _form_signals(_tokenize("두피마사지기")) == {"마사지기"}

    # 사전에 없는 단어라도 키워드 전체가 상품명에 그대로 들어있으면 매칭 인정
    # (형태 사전이 뷰티어휘 위주라 다른 카테고리를 못 잡던 문제, 2026-08).
    p, matched = find_matching_product(["청첩장스티커"], [_p("실링왁스 청첩장 스티커")])
    assert matched is True

    # 단, "~만들기"류는 재료 상품명에 문구가 그대로 들어가는 경우가 흔해서
    # (우레탄 줄이 "비즈팔찌만들기"로 오탐됐던 사례) 이 규칙에서 제외한다.
    p, matched = find_matching_product(["팔찌만들기"], [_p("탄성 우레탄 줄 비즈팔찌만들기 끈")])
    assert matched is False

    # 진짜 애매한 것(다른 종류 상품)은 여전히 불확실로 남아야 한다.
    p, matched = find_matching_product(["며느리발톱"], [_p("확대경 손톱깎이 파고드는발톱 손톱정리기")])
    assert matched is False

    print("ok")


if __name__ == "__main__":
    test()
