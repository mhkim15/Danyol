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
