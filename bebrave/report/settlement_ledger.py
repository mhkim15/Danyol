"""
정산 원장 — 로컬 저장 계층.

정산은 사후 변경(구매확정 후 취소 등)이 있고, 그럴 때 기존 레코드가 수정되는
게 아니라 같은 product_order_id로 다른 settle_type의 "새 레코드"가 생긴다
(bebrave/smartstore/settlement.py 모듈 docstring 참고). 변경감지(lastChanged)
API가 없으므로, 최근 구간을 통째로 재조회해서 upsert하는 방식으로만 최신
상태를 유지할 수 있다 — 단순 append(sales.py 방식)를 쓰면 취소로 인한 새
레코드가 기존 레코드와 별개로 쌓여 합계가 부풀 수 있다.
"""
import json
from pathlib import Path
from typing import List

from ..smartstore.settlement import CaseSettlement

SETTLEMENTS_LOG = Path("data/settlements.json")


def load_settlements() -> list:
    if not SETTLEMENTS_LOG.exists():
        return []
    with open(SETTLEMENTS_LOG, encoding="utf-8") as f:
        return json.load(f)


def _save(records: list) -> None:
    SETTLEMENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTLEMENTS_LOG, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def upsert_case_settlements(cases: List[CaseSettlement]) -> int:
    """건별 정산을 원장에 upsert. key = (product_order_id, settle_type) —
    같은 주문이 취소 등으로 새 settle_type 레코드가 생기면 별개 항목으로 남기고,
    동일 키는 최신 조회값으로 덮어써 재조회 시 중복이 쌓이지 않게 한다."""
    existing = load_settlements()
    by_key = {(r["product_order_id"], r["settle_type"]): r for r in existing}
    for c in cases:
        by_key[(c.product_order_id, c.settle_type)] = {
            "product_order_id": c.product_order_id,
            "settle_date": c.settle_date,
            "settle_amount": c.settle_amount,
            "commission_amount": c.commission_amount,
            "settle_type": c.settle_type,
        }
    _save(list(by_key.values()))
    return len(cases)


def _demo() -> None:
    """실행 가능한 자체 점검 — upsert가 중복을 만들지 않는지만 검증 (파일 IO는 임시경로)."""
    import tempfile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settlements.json"
        with patch(f"{__name__}.SETTLEMENTS_LOG", path):
            c1 = CaseSettlement("PO-1", "2026-08-20", settle_amount=1000, settle_type="NORMAL_SETTLE_ORIGINAL")
            upsert_case_settlements([c1])
            assert len(load_settlements()) == 1

            # 같은 키 재조회(금액 갱신) — 늘어나지 않고 덮어써야 함
            c1_updated = CaseSettlement("PO-1", "2026-08-20", settle_amount=1200, settle_type="NORMAL_SETTLE_ORIGINAL")
            upsert_case_settlements([c1_updated])
            records = load_settlements()
            assert len(records) == 1 and records[0]["settle_amount"] == 1200, "upsert가 갱신 대신 중복을 만듦"

            # 취소로 새 settle_type 레코드 — 별개 항목으로 남아야 함
            c2 = CaseSettlement("PO-1", "2026-08-21", settle_amount=-1200, settle_type="QUICK_SETTLE_ORIGINAL")
            upsert_case_settlements([c2])
            assert len(load_settlements()) == 2, "취소로 생긴 별개 settle_type이 기존 레코드를 덮어씀"

    print("settlement_ledger self-check OK")


if __name__ == "__main__":
    _demo()
