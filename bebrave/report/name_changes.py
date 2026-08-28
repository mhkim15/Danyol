"""
상품명 변경 전후 성과 비교 — 이름 재최적화(performance.html의 "이름 재최적화" 버튼)가
실제로 효과가 있었는지 판정한다. 변경 시점을 기록해두고, 그 앞뒤 구간의 주문 실적
(sales_orders.json)을 비교한다. registered_products.json은 현재 이름만 담는 스냅샷이라
이력이 안 남으므로 별도 원장이 필요하다.
"""
import json
from datetime import date
from pathlib import Path
from typing import Optional

NAME_CHANGES_LOG = Path("data/name_changes.json")


def load_name_changes() -> list:
    if not NAME_CHANGES_LOG.exists():
        return []
    with open(NAME_CHANGES_LOG, encoding="utf-8") as f:
        return json.load(f)


def record_name_change(naver_product_id: str, old_name: str, new_name: str) -> None:
    changes = load_name_changes()
    changes.append({
        "naver_product_id": naver_product_id,
        "old_name": old_name,
        "new_name": new_name,
        "changed_at": date.today().isoformat(),
    })
    NAME_CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(NAME_CHANGES_LOG, "w", encoding="utf-8") as f:
        json.dump(changes, f, ensure_ascii=False, indent=2)


def compare_before_after(naver_product_id: str, sales_records: list, changes: Optional[list] = None) -> Optional[dict]:
    """가장 최근 이름 변경 기준 전/후 주문건수·매출 비교. 변경 이력이 없으면 None."""
    changes = load_name_changes() if changes is None else changes
    product_changes = [c for c in changes if c["naver_product_id"] == naver_product_id]
    if not product_changes:
        return None
    latest = max(product_changes, key=lambda c: c["changed_at"])
    changed_at = latest["changed_at"]

    before = [r for r in sales_records if r.get("naver_product_id") == naver_product_id and r["date"] < changed_at]
    after = [r for r in sales_records if r.get("naver_product_id") == naver_product_id and r["date"] >= changed_at]

    return {
        "changed_at": changed_at,
        "old_name": latest["old_name"],
        "new_name": latest["new_name"],
        "before_orders": len(before),
        "before_revenue": sum(r["revenue"] for r in before),
        "after_orders": len(after),
        "after_revenue": sum(r["revenue"] for r in after),
    }


def _demo() -> None:
    """실행 가능한 자체 점검 — 전후 분리 계산만 검증 (파일 IO 없음)."""
    changes = [{"naver_product_id": "1", "old_name": "구이름", "new_name": "새이름", "changed_at": "2026-08-15"}]
    sales = [
        {"naver_product_id": "1", "date": "2026-08-10", "revenue": 1000},
        {"naver_product_id": "1", "date": "2026-08-20", "revenue": 3000},
        {"naver_product_id": "1", "date": "2026-08-25", "revenue": 3000},
        {"naver_product_id": "2", "date": "2026-08-20", "revenue": 9999},  # 다른 상품 — 섞이면 안 됨
    ]
    r = compare_before_after("1", sales, changes)
    assert r["before_orders"] == 1 and r["before_revenue"] == 1000
    assert r["after_orders"] == 2 and r["after_revenue"] == 6000
    assert compare_before_after("2", sales, changes) is None, "변경 이력 없는 상품은 None이어야 함"
    print("name_changes self-check OK")


if __name__ == "__main__":
    _demo()
