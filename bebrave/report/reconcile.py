"""
매출↔정산 대사 — 내가 계산한 매출(sales.py)과 실제 입금액(settlement_ledger.py)을
상품주문 단위로 대조한다.

config.py의 판매수수료(SALES_FEE_MAX=4%)·CS예비비(CS_RESERVE=2.5%)는 실적이
아니라 추정치다. 이 프로젝트의 마진 계산·판매가 산정·소싱 점수 전체가 이
가정 위에 서 있으므로, 실제 입금액과 대조해서 이 가정을 교정하면 시스템
전체의 정확도가 같이 올라간다.

주의: settle_amount(실정산액)는 "수수료 차감 후 실지급액"이고, sales.py의
revenue는 도매가를 빼기 전 판매금액이다 — 네이버는 도매가를 모르므로 이
둘의 차이(공제액)는 순수하게 "네이버가 뗀 돈"이지 내 순이익이 아니다.
그래서 도매가와 섞어 비교하지 않고, revenue 대비 공제율로만 비교한다.
"""
from typing import Optional

from .sales import load_orders as load_sales_orders
from .settlement_ledger import load_settlements


def reconcile(sales_records: Optional[list] = None, settlement_records: Optional[list] = None) -> list:
    """상품주문 단위 대사 결과 — 정산 데이터가 있는 주문만 반환(아직 정산 전이면 대상 아님)."""
    sales_records = load_sales_orders() if sales_records is None else sales_records
    settlement_records = load_settlements() if settlement_records is None else settlement_records

    settle_by_id = {}
    for s in settlement_records:
        pid = s["product_order_id"]
        # 같은 주문이 취소 등으로 여러 settle_type 레코드를 가질 수 있다 — 절대값이
        # 가장 큰 레코드(취소로 인한 0/음수 조정분이 아니라 원래 정산분일 가능성이 높음)를 우선한다.
        if pid not in settle_by_id or abs(s["settle_amount"]) > abs(settle_by_id[pid]["settle_amount"]):
            settle_by_id[pid] = s

    results = []
    for r in sales_records:
        pid = r["product_order_id"]
        s = settle_by_id.get(pid)
        if s is None:
            continue
        revenue = r["revenue"]
        settle_amount = s["settle_amount"]
        deduction = revenue - settle_amount
        results.append({
            "product_order_id": pid,
            "revenue": revenue,
            "settle_amount": settle_amount,
            "deduction": deduction,
            "deduction_rate": round(deduction / revenue, 4) if revenue else None,
            "settle_type": s.get("settle_type", ""),
            # 아래 넷은 양쪽 원장에 이미 있는데 결과로 옮기지 않아, 화면에 20자리 주문ID만
            # 남고 "무슨 상품이 언제 얼마나 떼였는지"를 사람이 알 수 없었다.
            "date": r.get("date", ""),
            "naver_product_id": r.get("naver_product_id", ""),
            "settle_date": s.get("settle_date", ""),
            # 네이버가 알려준 실제 수수료. deduction(매출−정산 역산)과 달리 실측값이다.
            "commission_amount": s.get("commission_amount"),
        })
    return results


def suggest_fee_rate(results: list) -> Optional[dict]:
    """대사 결과에서 평균 공제율을 역산해 config.py 가정과 비교. 표본 5건 미만이면
    신뢰할 수 없어 None(우연히 튄 한두 건으로 수수료율을 고치면 안 됨)."""
    rates = [r["deduction_rate"] for r in results if r["deduction_rate"] is not None]
    if len(rates) < 5:
        return None

    from ..config import ORDER_FEE, SALES_FEE_MAX, CS_RESERVE
    measured = sum(rates) / len(rates)
    assumed = ORDER_FEE + SALES_FEE_MAX + CS_RESERVE
    return {
        "sample_count": len(rates),
        "measured_rate": round(measured, 4),
        "assumed_rate": round(assumed, 4),
        "diff": round(measured - assumed, 4),
    }


def _demo() -> None:
    """실행 가능한 자체 점검 — 대사 매칭과 공제율 계산만 검증 (파일 IO 없음)."""
    sales = [
        {"product_order_id": "PO-1", "revenue": 10000, "profit": 2000,
         "date": "2026-08-10", "naver_product_id": "P1"},
        {"product_order_id": "PO-2", "revenue": 20000, "profit": 4000,
         "date": "2026-08-11", "naver_product_id": "P2"},
        {"product_order_id": "PO-3", "revenue": 10000, "profit": 2000},  # 정산 미매칭 — 제외돼야 함
    ]
    settlements = [
        {"product_order_id": "PO-1", "settle_amount": 9000, "settle_type": "NORMAL_SETTLE_ORIGINAL",
         "settle_date": "2026-08-13", "commission_amount": 900},
        {"product_order_id": "PO-2", "settle_amount": 18000, "settle_type": "NORMAL_SETTLE_ORIGINAL"},
    ]
    results = reconcile(sales, settlements)
    assert len(results) == 2, "정산 미매칭 주문이 섞여 들어옴"
    by_id = {r["product_order_id"]: r for r in results}
    assert by_id["PO-1"]["deduction"] == 1000 and by_id["PO-1"]["deduction_rate"] == 0.1
    assert by_id["PO-2"]["deduction_rate"] == 0.1

    # 화면이 "어떤 상품이 언제" 건인지 보여주려면 이 넷이 결과에 살아 있어야 한다.
    assert by_id["PO-1"]["date"] == "2026-08-10", "매출 날짜가 대사 결과에서 사라짐"
    assert by_id["PO-1"]["naver_product_id"] == "P1", "상품ID가 대사 결과에서 사라짐"
    assert by_id["PO-1"]["settle_date"] == "2026-08-13", "정산일이 대사 결과에서 사라짐"
    assert by_id["PO-1"]["commission_amount"] == 900, "실측 수수료가 대사 결과에서 사라짐"
    # 원장에 없는 값은 조용히 0으로 채우지 않는다 — 모르는 것과 0원은 다르다.
    assert by_id["PO-2"]["commission_amount"] is None and by_id["PO-2"]["settle_date"] == ""

    assert suggest_fee_rate(results) is None, "표본 2건인데 제안이 나옴 — 최소 표본 가드 실패"

    print("reconcile self-check OK")


if __name__ == "__main__":
    _demo()
