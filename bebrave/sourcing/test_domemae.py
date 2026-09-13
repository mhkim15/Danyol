"""최소 자가검증 — 프레임워크 없이 python3 -m bebrave.sourcing.test_domemae 로 실행."""
from .domemae import _form_signals, _tokenize, find_matching_product, is_accessory_name, DomemaeProduct, _parse_view_item


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

    # 부자재 오매칭 실증 사례 — "손톱영양제" 검색에 공병이, "파우더퍼프" 검색에
    # 보관 케이스가 최저가라서 뽑혔다(2026-09). 부자재 필터가 후보 선별
    # 단계에서 걸러야 한다.
    assert is_accessory_name("휴대용 3ml 큐티클 오일펜 공병 화장품 오일용기") is True
    assert is_accessory_name("메이크업 쿠션 파우더 퍼프 원형 보관 케이스") is True
    p, matched = find_matching_product(
        ["손톱영양제"],
        [_p("큐티클 오일펜 공병 2ml", price=100), _p("메니큐어 손톱영양제 네일강화제", price=1780)],
    )
    assert matched is True and "공병" not in p.name

    # 키워드 자체가 부자재 명칭이면(예: "화장품공병") 예외적으로 통과시킨다 —
    # 그럴 땐 부자재가 곧 완제품이다.
    assert is_accessory_name("휴대용 화장품 공병 세트", keyword="화장품공병") is False

    # 상세설명 이미지 사용 허용(desc.license.usable) — 허용된 상품만 쓸 수 있다(2026-09)
    view = lambda desc: _parse_view_item({"basis": {"no": "1"}, "desc": desc})
    assert view({"license": {"usable": True}}).image_usable is True
    assert view({"license": {"usable": False}}).image_usable is False, "불허 상품을 허용으로 읽음"
    assert view({}).image_usable is None, "허용 항목이 없는데 허용으로 봄"

    # 대문자 <IMG>·따옴표 없는 src도 상세이미지로 읽는다(일자손톱깎이: 대표사진 1장만 남던 문제)
    imgs = view({"contents": '<P align=center><IMG src="https://x/a_01.jpg"></p><img src=https://x/b.jpg>'}).images
    assert "https://x/a_01.jpg" in imgs and "https://x/b.jpg" in imgs, f"상세이미지를 빠뜨림: {imgs}"

    print("ok")


if __name__ == "__main__":
    test()
