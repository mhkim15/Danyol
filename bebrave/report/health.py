"""
스토어 헬스체크 — 새로 조회하지 않는다. 이미 흩어져 있던 판정 로직
(sync.py의 품절·마진붕괴, performance.py의 무판매, inquiries.py의 미답변,
claims.py의 반품률)을 한곳에 모아 심각도순으로 "오늘 할 일"을 만든다.

유일하게 새로 계산하는 건 발송 지연이다 — 결제완료 후 오래 미발송인 주문을
감시하는 코드가 지금까지 전혀 없었다(발송 지연은 굿서비스 점수에 직결).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

SEVERITY_URGENT = "긴급"
SEVERITY_WARN = "주의"
SEVERITY_INFO = "참고"
_SEVERITY_ORDER = {SEVERITY_URGENT: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}

DISPATCH_DELAY_HOURS = 24  # 결제완료 후 이 시간 넘게 미발송이면 지연으로 본다


@dataclass
class HealthIssue:
    severity: str
    category: str
    message: str
    detail: str = ""


def _dispatch_delay_issues() -> List[HealthIssue]:
    from ..smartstore.purchase_queue import load_queue, STATUS_ORDERED

    issues = []
    now = datetime.now()
    for i in load_queue():
        if i["status"] != STATUS_ORDERED:
            continue
        try:
            ordered_at = datetime.fromisoformat(i.get("ordered_at", ""))
        except ValueError:
            continue
        hours = (now - ordered_at).total_seconds() / 3600
        if hours >= DISPATCH_DELAY_HOURS:
            issues.append(HealthIssue(
                SEVERITY_URGENT, "발송지연",
                f"{i['product_name'][:24]} — 결제 후 {hours:.0f}시간째 미발송",
                f"주문 {i['product_order_id']}",
            ))
    return issues


def check_store_health() -> List[HealthIssue]:
    """각 신호는 독립적으로 실패해도 나머지 신호에 영향 없게 개별 try/except로 감싼다 —
    카카오 API 하나 막혔다고 품절 경고까지 안 보이면 안 된다."""
    issues: List[HealthIssue] = []

    try:
        from ..smartstore.sync import sync_all, ACTION_SUSPEND, ACTION_MARGIN_WARN, ACTION_ERROR
        for r in sync_all(dry_run=True):
            if r.action == ACTION_SUSPEND:
                issues.append(HealthIssue(SEVERITY_URGENT, "품절", f"{r.name[:24]} — {r.detail}"))
            elif r.action == ACTION_MARGIN_WARN:
                issues.append(HealthIssue(SEVERITY_WARN, "마진붕괴", f"{r.name[:24]} — {r.detail}"))
            elif r.action == ACTION_ERROR:
                issues.append(HealthIssue(SEVERITY_WARN, "확인실패", f"{r.name[:24]} — {r.detail}"))
    except Exception as e:
        issues.append(HealthIssue(SEVERITY_WARN, "동기화확인실패", str(e)))

    try:
        issues.extend(_dispatch_delay_issues())
    except Exception:
        pass

    try:
        from ..smartstore.auth import get_access_token
        from ..smartstore.inquiries import fetch_inquiries
        token = get_access_token()
        for q in fetch_inquiries(token, days=7, answered=False):
            issues.append(HealthIssue(SEVERITY_WARN, "미답변문의", f"{q.product_name[:24]} — {q.content[:30]}"))
    except Exception:
        pass

    try:
        from .claims import return_rate
        from ..config import FAST_SETTLEMENT_MAX_RETURN
        r = return_rate(days=30)
        if r["rate"] is not None and r["rate"] > FAST_SETTLEMENT_MAX_RETURN:
            issues.append(HealthIssue(
                SEVERITY_WARN, "반품률",
                f"최근 30일 반품률 {r['rate']:.0%} — 빠른정산 기준({FAST_SETTLEMENT_MAX_RETURN:.0%}) 초과",
                f"{r['claim_count']}건 / {r['order_count']}건",
            ))
    except Exception:
        pass

    try:
        from .performance import product_performance
        for p in product_performance():
            if p["status"] == "무판매(재검토 필요)":
                issues.append(HealthIssue(
                    SEVERITY_INFO, "무판매", f"{p['name'][:24]} — {p['days_since_registered']}일 경과"))
    except Exception:
        pass

    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    return issues


def _demo() -> None:
    """실행 가능한 자체 점검 — 발송지연 계산과 심각도 정렬만 검증 (네트워크 호출 없음)."""
    import tempfile
    import json
    from pathlib import Path
    from unittest.mock import patch
    from ..smartstore import purchase_queue as pq

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "purchase_queue.json"
        old_iso = (datetime.now().replace(microsecond=0) - timedelta(hours=30)).isoformat()
        path.write_text(json.dumps([
            {"product_order_id": "PO-1", "product_name": "지연상품", "status": pq.STATUS_ORDERED, "ordered_at": old_iso},
            {"product_order_id": "PO-2", "product_name": "정상상품", "status": pq.STATUS_ORDERED,
             "ordered_at": datetime.now().isoformat()},
        ]), encoding="utf-8")
        with patch.object(pq, "QUEUE_PATH", path):
            issues = _dispatch_delay_issues()
            assert len(issues) == 1 and "지연상품" in issues[0].message, "30시간 지연건을 못 잡음"

    unsorted = [
        HealthIssue(SEVERITY_INFO, "a", "m"),
        HealthIssue(SEVERITY_URGENT, "b", "m"),
        HealthIssue(SEVERITY_WARN, "c", "m"),
    ]
    unsorted.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    assert [i.severity for i in unsorted] == [SEVERITY_URGENT, SEVERITY_WARN, SEVERITY_INFO], "심각도 정렬 실패"

    print("health self-check OK")


if __name__ == "__main__":
    _demo()
