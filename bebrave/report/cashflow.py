"""
운전자본(현금 사이클) 타임라인 — 나간 돈(도매매 발주 지출)과 들어올 돈(정산예정)을
날짜순으로 합쳐 누적 잔액을 계산한다.

위탁판매는 도매매에 돈이 먼저 나가고 스마트스토어 정산은 나중에(보통 며칠~2주 뒤)
들어온다. 판매가 늘면 이 시차만큼 운전자금이 묶이는데, 지금까지는 이걸 한눈에 보는
화면이 없었다 — 발주 지출은 purchase_queue.json에, 입금 예정은 settlements.json에
따로 있어서 둘을 맞춰봐야 "지금 얼마가 비어 있는지"를 알 수 있다.

exact 잔액 예측이 목적이 아니다 — 방향과 시차 규모를 보는 용도.
"""
from typing import Optional

from ..smartstore.purchase_queue import load_queue, STATUS_ORDERED, STATUS_DISPATCHED
from .settlement_ledger import load_settlements


def cash_events(purchase_items: Optional[list] = None, settlement_records: Optional[list] = None) -> list:
    """지출·입금 이벤트를 날짜순으로 합친 뒤 누적잔액(balance)을 붙여 반환.
    amount는 지출이면 음수, 입금이면 양수."""
    purchase_items = load_queue() if purchase_items is None else purchase_items
    settlement_records = load_settlements() if settlement_records is None else settlement_records

    events = []
    for i in purchase_items:
        if i.get("status") not in (STATUS_ORDERED, STATUS_DISPATCHED):
            continue
        amount = i.get("spent_amount")
        if amount is None:  # 매칭 실패한 수동발주 등 지출액 미상 — 타임라인에서 제외(0 오판정 방지)
            continue
        events.append({
            "date": (i.get("updated_at") or "")[:10],
            "type": "지출",
            "label": i.get("product_name", ""),
            "amount": -amount,
        })

    for s in settlement_records:
        date_str = s.get("settle_date", "")
        amount = s.get("settle_amount", 0)
        if not date_str:
            continue
        events.append({
            "date": date_str,
            "type": "입금예정",
            "label": f"정산 {s.get('product_order_id', '')}",
            "amount": amount,
        })

    events = [e for e in events if e["date"]]
    events.sort(key=lambda e: e["date"])

    balance = 0
    for e in events:
        balance += e["amount"]
        e["balance"] = balance
    return events


def _demo() -> None:
    """실행 가능한 자체 점검 — 정렬·누적잔액 계산만 검증 (파일 IO 없음)."""
    purchase_items = [
        {"status": STATUS_ORDERED, "updated_at": "2026-08-10", "product_name": "A", "spent_amount": 3000},
        {"status": "ready", "updated_at": "2026-08-11", "product_name": "무시대상", "spent_amount": 9999},
        {"status": STATUS_DISPATCHED, "updated_at": "2026-08-12", "product_name": "B", "spent_amount": None},  # 미상 제외
    ]
    settlements = [
        {"settle_date": "2026-08-15", "settle_amount": 5000, "product_order_id": "PO-1"},
    ]
    events = cash_events(purchase_items, settlements)
    assert len(events) == 2, "ready 상태나 지출액 미상 건이 섞이면 안 됨"
    assert events[0]["date"] == "2026-08-10" and events[0]["balance"] == -3000
    assert events[1]["date"] == "2026-08-15" and events[1]["balance"] == 2000, "누적잔액 계산 오류"
    print("cashflow self-check OK")


if __name__ == "__main__":
    _demo()
