"""최소 자가검증 — 프레임워크 없이 python3 -m bebrave.sourcing.test_discover_scoring 로 실행."""
from .discover import _entry_score, _profit_score, _cap_uncertain_recommendation, DiscoveryResult, to_product_candidates
from .models import registration_block_reason
from ..margin.calculator import estimate_sale_price, calculate as calc_margin
from ..config import TARGET_MARGIN, MIN_ABS_PROFIT


def test_cap_uncertain_recommendation():
    # 형태 일치 확인됐고 도매가도 있으면 등급 그대로.
    assert _cap_uncertain_recommendation("진입 권장", supply_match_uncertain=False, supply_price=6000) == "진입 권장"
    # 매칭 불확실하면 등급 안 가리고 "보류"로 하드 캡.
    assert _cap_uncertain_recommendation("진입 권장", supply_match_uncertain=True, supply_price=6000) == "보류"
    # 도매가 자체가 없어도(도매매 조회 실패 등) 마찬가지.
    assert _cap_uncertain_recommendation("진입 가능", supply_match_uncertain=False, supply_price=0) == "보류"
    # 이미 "제외"인 건 "보류"로 격상시키지 않는다.
    assert _cap_uncertain_recommendation("제외", supply_match_uncertain=True, supply_price=0) == "제외"
    print("ok")


def test_estimate_sale_price_shipping_gap():
    """4-3의 계산식 버그 재현 케이스 — 목표마진 분기가 배송비를 빠뜨려서 도매가
    20,900원 이상이 전부 마진 미달로 탈락했다. 수정 후 값(문서 표와 동일)을 고정."""
    assert estimate_sale_price(20_900) == 34_300
    assert estimate_sale_price(25_000) == 40_100
    assert estimate_sale_price(40_000) == 61_600
    print("ok")


def test_registration_block_reason():
    # 실물확인 + 자동판정 둘 다 통과해야 등록 가능.
    assert registration_block_reason(supply_matched=True, human_confirmed=True) == ""
    # 사람이 실물을 안 봤으면 자동판정이 맞아도 막는다.
    assert registration_block_reason(supply_matched=True, human_confirmed=False) != ""
    # 사람이 확인했어도 자동판정이 불일치를 의심하면 막는다.
    assert registration_block_reason(supply_matched=False, human_confirmed=True) != ""
    # 둘 다 미확인이면 당연히 막는다.
    assert registration_block_reason(supply_matched=None, human_confirmed=False) != ""
    print("ok")


def test_to_product_candidates_preserves_track():
    """저장 변환 시 트랙·판정·월검색수가 사라지지 않는지 (2026-08 회귀 수정 검증) —
    전에는 monthly_search가 0으로 덮어써지고 track/recommendation이 아예 안 옮겨졌다."""
    results = [
        DiscoveryResult(keyword="테스트키워드", category="주방용품", score=62, recommendation="진입 권장",
                         product_count=0, monthly_search=3200, trend_direction="up", is_seasonal=False,
                         competition_barrier="low", supply_tier="tight", avg_naver_price=12000, supply_price=4000,
                         supply_name="테스트상품", margin_rate=0.22, margin_passes=True, track="A"),
        DiscoveryResult(keyword="테스트키워드", category="주방용품", score=68, recommendation="리메이크 권장",
                         product_count=0, monthly_search=3200, trend_direction="up", is_seasonal=False,
                         competition_barrier="mid", supply_tier="normal", avg_naver_price=12000, supply_price=4000,
                         supply_name="테스트상품", margin_rate=0.22, margin_passes=True, track="B"),
    ]
    candidates = to_product_candidates(results)
    assert len(candidates) == 2
    assert {c.track for c in candidates} == {"A", "B"}, "트랙이 저장 단계에서 사라짐"
    assert all(c.monthly_search == 3200 for c in candidates), "월검색수가 0으로 덮어써짐"
    assert candidates[0].recommendation == "진입 권장" and candidates[1].recommendation == "리메이크 권장"

    # (키워드, 트랙) 중복 제거 — 같은 키워드의 두 트랙이 서로를 밀어내면 안 된다.
    existing_kw = set()
    kept = []
    for c in candidates:
        key = (c.keyword, c.track)
        if key not in existing_kw:
            existing_kw.add(key)
            kept.append(c)
    assert len(kept) == 2, "같은 키워드의 리메이크 후보가 조용히 사라짐"

    print("ok")


def test():
    # 매칭 실패(False)는 더 이상 감점하지 않는다 — 매칭 사전이 카테고리를
    # 다 못 커버해서 실제로 맞는 매칭도 False로 뜨는 경우가 흔했기 때문.
    assert _entry_score("tight", "A", supply_matched=False) == _entry_score("tight", "A", supply_matched=None)
    # 확인된 매칭(True)은 여전히 가점.
    assert _entry_score("tight", "A", supply_matched=True) > _entry_score("tight", "A", supply_matched=None)
    # tier 순서는 tight > normal > loose 유지.
    assert _entry_score("tight", "A") > _entry_score("normal", "A") > _entry_score("loose", "A")

    # 도매가에 목표마진을 얹어 역산한 판매가는 실제로 목표 마진율 근처를 통과해야 한다.
    # 20,900/25,000/40,000원은 무료배송 기준선(3만원)을 막 넘기는 구간 — 목표마진
    # 분기가 배송비를 안 반영해서 이 구간 전체가 마진 미달로 탈락하던 버그가 있었다
    # (2026-09 발견·수정, estimate_sale_price 참고).
    for cost in (130, 500, 2_500, 11_320, 20_900, 25_000, 40_000, 134_000):
        sale = estimate_sale_price(cost)
        result = calc_margin(sale_price=sale, cost_price=cost, free_shipping=(sale >= 30_000))
        assert result.margin_rate >= TARGET_MARGIN - 0.03, (cost, sale, result.margin_rate)
        # 저가 상품도 절대이익 하한(5,000원)을 실제로 넘겨야 한다 — 이게 이번 수정의 핵심.
        assert result.passes_abs_floor, (cost, sale, result.net_profit)

    # 목표 통과 시 마진율 차이가 점수에 그대로 반영돼야 한다(71%와 87%가 더 이상 동점 X).
    assert _profit_score(True, 0.865) > _profit_score(True, 0.715) > _profit_score(True, 0.21)
    # 미조회(중립)·미달(고정 저점)은 기존 동작 유지.
    assert _profit_score(False, 0.0) == 50
    assert _profit_score(False, 0.15) == 20
    # 100% 넘는 마진율도 100점을 넘지 않는다.
    assert _profit_score(True, 1.5) == 100

    print("ok")


if __name__ == "__main__":
    test()
    test_to_product_candidates_preserves_track()
    test_cap_uncertain_recommendation()
    test_estimate_sale_price_shipping_gap()
    test_registration_block_reason()
