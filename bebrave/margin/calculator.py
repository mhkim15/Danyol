"""
마진 계산기 — 2026 네이버 스마트스토어 수수료 체계 기준.

실질 순이익 = 판매가
             - 도매가(공급가)
             - 주문관리수수료 3.63%
             - 판매수수료 1~4% (유입 경로별 상이)
             - 배송비 (무료배송 시 판매자 부담)
             - 반품·CS 예비비 (약 2~3%)
"""
from dataclasses import dataclass

from ..config import (
    ORDER_FEE,
    SALES_FEE_MIN,
    SALES_FEE_MAX,
    CS_RESERVE,
    SHIPPING_FEE,
    FREE_SHIPPING_THRESHOLD,
    MIN_MARGIN,
    TARGET_MARGIN,
    MIN_ABS_PROFIT,
)


@dataclass
class MarginResult:
    sale_price: int
    cost_price: int
    order_fee: int
    sales_fee: int
    shipping_cost: int
    cs_reserve: int
    net_profit: int
    margin_rate: float
    passes_min: bool
    passes_target: bool
    passes_abs_floor: bool   # 절대이익 하한(MIN_ABS_PROFIT) 통과 여부

    def summary(self) -> str:
        if not self.passes_abs_floor:
            status = "절대이익부족"
        elif self.passes_target:
            status = "목표달성"
        elif self.passes_min:
            status = "최소통과"
        else:
            status = "마진부족"
        return (
            f"[{status}] 순이익: {self.net_profit:,}원 / 마진율: {self.margin_rate:.1%}\n"
            f"  주문관리수수료: {self.order_fee:,}원 | 판매수수료: {self.sales_fee:,}원 | "
            f"배송: {self.shipping_cost:,}원 | CS예비비: {self.cs_reserve:,}원"
        )


def estimate_sale_price(cost_price: int) -> int:
    """
    도매가에서 목표 마진율(TARGET_MARGIN)과 최소 절대이익(MIN_ABS_PROFIT)을
    동시에 만족하는 판매가를 역산 (100원 단위 올림) — 둘 중 더 높은 쪽을 쓴다.

    원래는 목표 마진율만 딱 채우는 최소가를 썼는데, 저가 상품(도매가 1만원대
    이하)은 %마진을 채워도 절대이익 5,000원을 못 넘는 경우가 대부분이었다
    (2026-08 실측: 도매매 표본 100개 중 83%가 도매가 1만원 미만, 그중 %마진만
    맞춘 가격으로는 95%가 절대이익 하한 미달). 판매가를 절대이익 하한을 채우는
    수준까지 더 올리면(마진율은 오히려 더 좋아짐) 상당수가 통과권에 들어온다.

    네이버 경쟁상품가 조회가 불가능해진 뒤(2026-07-31 API 종료) 판매가 추정의
    유일한 근거로 씀 — 발굴 단계(discover.py)와 실제 등록 단계(pipeline.py)가
    동일한 공식을 쓰도록 통일.
    """
    fee_rate = ORDER_FEE + SALES_FEE_MAX + CS_RESERVE

    target_denom = 1 - fee_rate - TARGET_MARGIN
    target_price = int(cost_price / target_denom) + 1

    floor_denom = 1 - fee_rate
    shipping = 0
    floor_price = int((MIN_ABS_PROFIT + cost_price + shipping) / floor_denom) + 1
    if floor_price >= FREE_SHIPPING_THRESHOLD and shipping == 0:
        shipping = SHIPPING_FEE
        floor_price = int((MIN_ABS_PROFIT + cost_price + shipping) / floor_denom) + 1

    price = max(target_price, floor_price)
    return ((price // 100) + 1) * 100


def calculate(
    sale_price: int,
    cost_price: int,
    sales_fee_rate: float = SALES_FEE_MAX,
    free_shipping: bool = False,
) -> MarginResult:
    """
    Args:
        sale_price: 판매가 (원)
        cost_price: 도매가 / 공급가 (원)
        sales_fee_rate: 판매수수료율 (기본 최대치 4% 적용 — 보수적 계산)
        free_shipping: 무료배송 여부 (True면 배송비 판매자 부담)
    """
    order_fee_amt = round(sale_price * ORDER_FEE)
    sales_fee_amt = round(sale_price * sales_fee_rate)
    shipping_cost = SHIPPING_FEE if free_shipping or sale_price >= FREE_SHIPPING_THRESHOLD else 0
    cs_reserve_amt = round(sale_price * CS_RESERVE)

    net_profit = sale_price - cost_price - order_fee_amt - sales_fee_amt - shipping_cost - cs_reserve_amt
    margin_rate = net_profit / sale_price if sale_price > 0 else 0.0

    return MarginResult(
        sale_price=sale_price,
        cost_price=cost_price,
        order_fee=order_fee_amt,
        sales_fee=sales_fee_amt,
        shipping_cost=shipping_cost,
        cs_reserve=cs_reserve_amt,
        net_profit=net_profit,
        margin_rate=round(margin_rate, 4),
        passes_min=margin_rate >= MIN_MARGIN,
        passes_target=margin_rate >= TARGET_MARGIN,
        passes_abs_floor=net_profit >= MIN_ABS_PROFIT,
    )
