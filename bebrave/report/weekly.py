"""
주간 리포트 — 매주 월요일 확인용 체크리스트 출력.

가능한 항목(주문건수, 미판매/자동삭제 위험 상품)은 실데이터로 자동 채움.
굿서비스 점수·반품률은 네이버 커머스 API에 조회 엔드포인트가 없어 수동 확인 유지.
"""
from datetime import date
from pathlib import Path

from ..config import (
    FAST_SETTLEMENT_MIN_ORDERS,
    FAST_SETTLEMENT_MAX_RETURN,
    GOOD_SERVICE_MIN,
    STALE_PRODUCT_MONTHS,
    AUTO_DELETE_MONTHS,
)


def _live_order_count() -> str:
    """이번 달 주문건수 — 커머스 API 키 있으면 실조회, 없으면 수동 확인 안내."""
    try:
        from ..smartstore.auth import get_access_token
        from ..smartstore.orders import fetch_new_orders
        token = get_access_token()
        orders = fetch_new_orders(token, hours=24 * 30)
        return f"{len(orders)}건 (최근 30일, 자동조회)"
    except Exception:
        return f"수동 확인 필요 (빠른정산 기준: {FAST_SETTLEMENT_MIN_ORDERS}건)"


def _live_stale_products() -> tuple[str, str]:
    """미판매/자동삭제 위험 상품 — tracker 데이터 있으면 실조회."""
    path = Path("data/tracked_products.json")
    if not path.exists():
        return (
            f"수동 확인 필요 ({STALE_PRODUCT_MONTHS}개월 이상 미판매 교체 검토)",
            f"수동 확인 필요 ({AUTO_DELETE_MONTHS}개월 이상 미판매 → 자동삭제 위험)",
        )
    try:
        from ..tracker.products import ProductTracker
        tracker = ProductTracker(path)
        stale = tracker.stale_products()
        risky = tracker.auto_delete_risk()
        stale_line = f"{len(stale)}건" if stale else "없음"
        risky_line = f"{len(risky)}건 — {', '.join(p.name for p in risky[:3])}" if risky else "없음"
        return stale_line, risky_line
    except Exception:
        return "조회 실패", "조회 실패"


def weekly_summary() -> str:
    today = date.today()
    order_count = _live_order_count()
    stale_line, risky_line = _live_stale_products()

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  비브레이브 주간 체크리스트 ({today})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] 1. 굿서비스 점수 확인 (목표: {GOOD_SERVICE_MIN}점 이상, 수동 확인 — API 미제공)
[ ] 2. 이번 달 주문건수: {order_count}
[ ] 3. 반품률 확인 (빠른정산 기준: {FAST_SETTLEMENT_MAX_RETURN:.0%} 미만, 수동 확인 — API 미제공)
[ ] 4. {STALE_PRODUCT_MONTHS}개월 미판매 상품 교체 검토: {stale_line}
[ ] 5. {AUTO_DELETE_MONTHS}개월 자동삭제 위험 상품: {risky_line}
[ ] 6. 실질 마진율 재계산 (수수료 변경 반영 여부)
[ ] 7. 신규 소싱 후보 2~3개 발굴 (아이템스카우트)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  발굴 후보 메뉴에서 현재 후보 확인
  판매추적 메뉴에서 4·5번 상세 확인
  판매추적 메뉴의 "최근 30일 주문으로 동기화" 버튼으로 판매일 데이터 갱신 (커머스 API 필요)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
