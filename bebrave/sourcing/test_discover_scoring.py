"""최소 자가검증 — 프레임워크 없이 python3 -m bebrave.sourcing.test_discover_scoring 로 실행."""
from .discover import _entry_score, _profit_score
from ..margin.calculator import estimate_sale_price, calculate as calc_margin
from ..config import TARGET_MARGIN, MIN_ABS_PROFIT


def test():
    # 매칭 실패(False)는 더 이상 감점하지 않는다 — 매칭 사전이 카테고리를
    # 다 못 커버해서 실제로 맞는 매칭도 False로 뜨는 경우가 흔했기 때문.
    assert _entry_score("tight", "A", supply_matched=False) == _entry_score("tight", "A", supply_matched=None)
    # 확인된 매칭(True)은 여전히 가점.
    assert _entry_score("tight", "A", supply_matched=True) > _entry_score("tight", "A", supply_matched=None)
    # tier 순서는 tight > normal > loose 유지.
    assert _entry_score("tight", "A") > _entry_score("normal", "A") > _entry_score("loose", "A")

    # 도매가에 목표마진을 얹어 역산한 판매가는 실제로 목표 마진율 근처를 통과해야 한다.
    # ponytail: 역산식은 배송비를 %비용으로 뭉뚱그려 근사한다(pipeline.py 기존 방식
    # 그대로 승계) — 무료배송 구간(3만원 이상)에서 실제 마진이 1~2%p 낮게 나올 수
    # 있음. 배송비를 별도 항으로 반영하는 정밀 역산이 필요해지면 그때 고칠 것.
    for cost in (130, 500, 2_500, 11_320, 134_000):
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
