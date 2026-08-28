"""
반품·취소·교환(클레임) 누적 원장.

지금까지 /cs 화면이 CLAIM_REQUESTED 상태변경 건을 커머스 API로 조회만 하고
버리고 있었다 — 매번 최근 N시간 스냅샷만 보여주고 저장이 없었다. 반품률은
빠른정산 자격(월 20% 미만, config.FAST_SETTLEMENT_MAX_RETURN) 판정 기준이자
스토어 헬스체크의 입력값인데, 저장을 시작하지 않으면 과거분은 영원히
복구할 수 없다. sales.py의 record_orders() 패턴을 그대로 따른다.
"""
import json
from datetime import date, timedelta
from pathlib import Path

CLAIMS_LOG = Path("data/claims.json")
SALES_ORDERS_LOG = Path("data/sales_orders.json")


def load_claims() -> list:
    if not CLAIMS_LOG.exists():
        return []
    with open(CLAIMS_LOG, encoding="utf-8") as f:
        return json.load(f)


def _save_claims(records: list) -> None:
    CLAIMS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CLAIMS_LOG, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def record_claims(orders: list) -> int:
    """이미 조회해온 ProductOrder(클레임) 목록을 원장에 반영 (product_order_id로 중복방지).
    /cs 방문 시 조회하는 김에 같이 호출해서 별도 버튼 없이 쌓이게 한다."""
    today = date.today()
    existing = load_claims()
    existing_ids = {r["product_order_id"] for r in existing}
    added = 0
    for o in orders:
        if o.product_order_id in existing_ids:
            continue
        claimed_date = (o.ordered_at or "")[:10] or today.isoformat()
        existing.append({
            "product_order_id": o.product_order_id,
            "naver_product_id": getattr(o, "product_id", ""),
            "product_name": o.product_name,
            "claim_type": o.claim_type or o.status,
            "claim_reason": o.claim_reason,
            "quantity": o.quantity,
            "claimed_at": claimed_date,
            "recorded_at": today.isoformat(),
        })
        existing_ids.add(o.product_order_id)
        added += 1

    if added:
        _save_claims(existing)
    return added


def return_rate(days: int = 30) -> dict:
    """최근 N일 반품률 = 클레임 건수 ÷ 주문 건수. 분모(주문)가 없으면 계산 불가로 반환.
    빠른정산 반품률 기준(config.FAST_SETTLEMENT_MAX_RETURN)과 비교하는 용도."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    claims = [c for c in load_claims() if c["claimed_at"] >= cutoff]

    orders = []
    if SALES_ORDERS_LOG.exists():
        with open(SALES_ORDERS_LOG, encoding="utf-8") as f:
            orders = [r for r in json.load(f) if r["date"] >= cutoff]

    if not orders:
        return {"rate": None, "claim_count": len(claims), "order_count": 0}

    return {
        "rate": round(len(claims) / len(orders), 4),
        "claim_count": len(claims),
        "order_count": len(orders),
    }


def _demo() -> None:
    """실행 가능한 자체 점검 — 중복방지·반품률 계산만 검증 (파일 IO 없음, 모듈 경로만 임시 치환)."""
    import tempfile
    from dataclasses import dataclass
    from unittest.mock import patch

    @dataclass
    class FakeOrder:
        product_order_id: str
        product_name: str
        status: str
        claim_type: str = ""
        claim_reason: str = ""
        quantity: int = 1
        ordered_at: str = ""
        product_id: str = ""

    with tempfile.TemporaryDirectory() as tmp:
        claims_path = Path(tmp) / "claims.json"
        sales_path = Path(tmp) / "sales_orders.json"
        with patch(f"{__name__}.CLAIMS_LOG", claims_path), patch(f"{__name__}.SALES_ORDERS_LOG", sales_path):
            o1 = FakeOrder("PO-1", "실리콘주걱", "CANCELED", claim_type="CANCEL", ordered_at="2026-08-10")
            added = record_claims([o1, o1])  # 같은 건 두 번 — 중복 방지 확인
            assert added == 1, "중복 클레임이 두 번 저장됨"

            sales_path.write_text(json.dumps([
                {"date": "2026-08-10", "revenue": 1000, "profit": 100},
                {"date": "2026-08-11", "revenue": 1000, "profit": 100},
                {"date": "2026-08-11", "revenue": 1000, "profit": 100},
                {"date": "2026-08-11", "revenue": 1000, "profit": 100},
            ]), encoding="utf-8")
            r = return_rate(days=30)
            assert r["claim_count"] == 1 and r["order_count"] == 4 and r["rate"] == 0.25, \
                f"반품률 계산 오류: {r}"

    print("claims self-check OK")


if __name__ == "__main__":
    _demo()
