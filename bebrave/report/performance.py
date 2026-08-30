"""
상품별 판매 성과 — 잘 팔리는/안 팔리는 상품을 구분하기 위한 집계.

노출/클릭 통계(스마트스토어 비즈어드바이저)는 이 코드베이스에 연동돼 있지 않아
"왜 안 팔리는가"의 3단 분류(노출 안 됨 / 클릭 없음 / 클릭되나 안 팔림)까지는 아직
못 간다 — 그 축을 넣으려면 별도 통계 API 연동이 필요하다(2026-08 기준 미착수).
지금 낼 수 있는 답은 "얼마나 팔렸는가"까지다: 등록 후 경과일 대비 주문·매출·
순수익, 그리고 무판매 여부.

원가 매칭 실패(profit=None, sales.py 참고)로 인한 순수익 미상 건수는 uncertain_count로
따로 노출한다 — 순수익 0인지 계산 불가인지 구분 못 하면 "잘 안 팔리는 상품"을
오판할 수 있다.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..config import STALE_PRODUCT_MONTHS, SALES_TIER_DAYS, SALES_TIER_LOW_MAX, SALES_TIER_GOOD_MAX

REGISTERED_PATH = Path("data/registered_products.json")
SALES_PATH = Path("data/sales_orders.json")

NEW_PRODUCT_DAYS = 14  # 이 기간 내엔 무주문이어도 "신규 관찰중"으로 보고 무판매 판정을 유예


def _load(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def product_performance(sales_records: Optional[list] = None, registered: Optional[list] = None,
                         today: Optional[date] = None) -> list:
    """등록 상품별 주문건수/매출/순수익/경과일/상태를 집계해 매출 내림차순으로 반환."""
    sales_records = _load(SALES_PATH) if sales_records is None else sales_records
    registered = _load(REGISTERED_PATH) if registered is None else registered
    today = today or date.today()

    by_pid = {}
    for r in sales_records:
        pid = r.get("naver_product_id", "")
        if not pid:
            continue
        b = by_pid.setdefault(pid, {"order_count": 0, "revenue": 0, "profit": 0, "uncertain_count": 0})
        b["order_count"] += 1
        b["revenue"] += r.get("revenue", 0)
        if r.get("profit") is None:
            b["uncertain_count"] += 1
        else:
            b["profit"] += r["profit"]

    stale_days = STALE_PRODUCT_MONTHS * 30
    results = []
    for p in registered:
        pid = p.get("naver_product_id", "")
        agg = by_pid.get(pid, {"order_count": 0, "revenue": 0, "profit": 0, "uncertain_count": 0})
        try:
            days = (today - date.fromisoformat(p.get("registered_date", ""))).days
        except ValueError:
            days = None

        if agg["order_count"] > 0:
            status = "판매중"
        elif days is not None and days < NEW_PRODUCT_DAYS:
            status = "신규(관찰중)"
        elif days is not None and days >= stale_days:
            status = "무판매(재검토 필요)"
        else:
            status = "무판매"

        results.append({
            "naver_product_id": pid,
            "name": p.get("name", ""),
            "registered_date": p.get("registered_date", ""),
            "days_since_registered": days,
            "order_count": agg["order_count"],
            "revenue": agg["revenue"],
            "profit": agg["profit"],
            "uncertain_count": agg["uncertain_count"],
            "status": status,
        })

    results.sort(key=lambda r: r["revenue"], reverse=True)
    return results


def recent_order_counts(sales_records: Optional[list] = None, days: int = SALES_TIER_DAYS,
                         today: Optional[date] = None) -> dict:
    """최근 N일간 상품별 주문건수 — "상품 상태"(판매 등급) 판정용 분자.
    전체기간 누적(product_performance의 order_count)과 달리 최근 흐름만 본다 —
    예전엔 잘 팔렸어도 최근에 안 팔리면 낮은 등급으로 잡혀야 하기 때문."""
    sales_records = _load(SALES_PATH) if sales_records is None else sales_records
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()

    counts = {}
    for r in sales_records:
        pid = r.get("naver_product_id", "")
        if not pid or r.get("date", "") < cutoff:
            continue
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def sales_tier(recent_count: int) -> str:
    """최근 판매건수 → 판매 등급. 0건은 "저조"보다 심각하게 본다 — 노출 자체가
    끊겼을 가능성이 있어 점검이 더 급하다는 판단."""
    if recent_count == 0:
        return "점검필요"
    if recent_count < SALES_TIER_LOW_MAX:
        return "저조"
    if recent_count < SALES_TIER_GOOD_MAX:
        return "양호"
    return "인기"


def _demo() -> None:
    sales = [
        {"naver_product_id": "1", "revenue": 3000, "profit": 500},
        {"naver_product_id": "1", "revenue": 3000, "profit": None},
    ]
    registered = [
        {"naver_product_id": "1", "name": "A", "registered_date": "2026-07-01"},
        {"naver_product_id": "2", "name": "B", "registered_date": "2026-08-10"},
        {"naver_product_id": "3", "name": "C", "registered_date": "2026-04-01"},
    ]
    results = product_performance(sales, registered, today=date(2026, 8, 15))
    by_id = {r["naver_product_id"]: r for r in results}
    assert by_id["1"]["order_count"] == 2 and by_id["1"]["revenue"] == 6000 and by_id["1"]["profit"] == 500
    assert by_id["1"]["uncertain_count"] == 1 and by_id["1"]["status"] == "판매중"
    assert by_id["2"]["status"] == "신규(관찰중)"          # 5일 경과, 무주문
    assert by_id["3"]["status"] == "무판매(재검토 필요)"    # 136일 경과(>90일=3개월), 무주문

    recent = [
        {"naver_product_id": "A", "date": "2026-08-14"},
        {"naver_product_id": "B", "date": "2026-08-10"}, {"naver_product_id": "B", "date": "2026-08-11"},
        {"naver_product_id": "B", "date": "2026-08-12"}, {"naver_product_id": "B", "date": "2026-08-13"},
        {"naver_product_id": "B", "date": "2026-08-14"}, {"naver_product_id": "B", "date": "2026-08-15"},
        {"naver_product_id": "C", "date": "2026-01-01"},  # 30일 밖 — 집계에서 빠져야 함
    ]
    counts = recent_order_counts(recent, days=30, today=date(2026, 8, 15))
    assert counts == {"A": 1, "B": 6}, f"최근 주문건수 집계 오류: {counts}"
    assert sales_tier(0) == "점검필요" and sales_tier(1) == "저조" and sales_tier(counts["A"]) == "저조"
    assert sales_tier(counts["B"]) == "양호" and sales_tier(20) == "인기"

    print("performance self-check OK")


if __name__ == "__main__":
    _demo()
