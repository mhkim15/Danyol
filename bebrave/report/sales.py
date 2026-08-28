"""
매출/순수익 집계 — 홈 대시보드 판매 현황(월별 일단위 꺾은선) 차트용.

네이버 커머스 API는 날짜 범위 조회가 아니라 "최근 N시간 내 상태변경" 방식이라
매 페이지 로드마다 조회하면 느리다. 그래서 "매출 새로고침" 버튼을 누를 때만
이번달 주문을 가져와 data/sales_orders.json(주문 단위 원장)에 추가하고,
홈 화면은 그 원장을 읽어 선택된 연/월의 일별 매출로 집계만 한다 — 월 이동은
API 재호출 없이 로컬 데이터로 바로 계산.
"""
import calendar
import json
from datetime import date
from pathlib import Path

from ..margin.calculator import calculate as calc_margin

SALES_ORDERS_LOG = Path("data/sales_orders.json")
REGISTERED_PRODUCTS = Path("data/registered_products.json")


def _load_registered() -> list:
    if not REGISTERED_PRODUCTS.exists():
        return []
    with open(REGISTERED_PRODUCTS, encoding="utf-8") as f:
        return json.load(f)


def _match_registered(order, registered: list) -> tuple:
    """(등록상품, 매칭방법 'id'|'name'|'none'). 상품ID 매칭이 최우선 —
    이름 부분일치만으로는 다른 상품인데 이름이 비슷해 원가를 잘못 가져올 수 있다."""
    product_id = getattr(order, "product_id", "")
    if product_id:
        for p in registered:
            if p.get("naver_product_id") and str(p["naver_product_id"]) == str(product_id):
                return p, "id"
    for p in registered:
        name = p.get("name", "")
        if name and (name in order.product_name or order.product_name in name):
            return p, "name"
    return {}, "none"


def load_orders() -> list:
    if not SALES_ORDERS_LOG.exists():
        return []
    with open(SALES_ORDERS_LOG, encoding="utf-8") as f:
        return json.load(f)


def _save_orders(records: list) -> None:
    SALES_ORDERS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SALES_ORDERS_LOG, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def record_orders(orders: list) -> int:
    """이미 조회해온 ProductOrder 목록을 원장에 반영 (product_order_id로 중복방지).
    홈 화면 로드 시 처리할 주문을 조회하는 김에 같이 호출해서, 매출 새로고침을
    위한 별도 버튼/API 호출 없이도 방문할 때마다 최신 데이터가 쌓이게 한다."""
    today = date.today()
    registered = _load_registered()
    existing = load_orders()
    existing_ids = {r["product_order_id"] for r in existing}
    added = 0
    for o in orders:
        if o.product_order_id in existing_ids:
            continue
        line_revenue = o.unit_price * o.quantity
        matched, method = _match_registered(o, registered)
        if matched:
            m = calc_margin(sale_price=o.unit_price, cost_price=matched.get("supply_price", 0))
            profit = m.net_profit * o.quantity
        else:
            # 등록 상품과 매칭 안 되면 원가를 몰라 순수익 계산 불가 — 0이 아니라 None(미상)으로
            # 구분해야 한다. 0으로 두면 "매출은 있는데 이익이 0"이라는 잘못된 대시보드가 된다.
            profit = None
        order_date = (o.ordered_at or "")[:10] or today.isoformat()
        existing.append({
            "product_order_id": o.product_order_id,
            "date": order_date,
            "revenue": line_revenue,
            "profit": profit,
            "naver_product_id": matched.get("naver_product_id", "") if matched else "",
            "match_method": method,
        })
        existing_ids.add(o.product_order_id)
        added += 1

    if added:
        _save_orders(existing)
    return added


def month_series(records: list, year: int, month: int) -> list:
    """선택된 연/월의 1일부터 말일까지 일별 매출/순수익/주문건수.
    profit이 None(원가 매칭 실패)인 주문은 순수익 합계에서 제외하고 uncertain_count로 따로 센다 —
    매출은 잡히는데 이익이 0으로 보이는 착시를 막기 위함."""
    days_in_month = calendar.monthrange(year, month)[1]
    daily = {d: {"revenue": 0, "profit": 0, "order_count": 0, "uncertain_count": 0} for d in range(1, days_in_month + 1)}
    for r in records:
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        if d.year == year and d.month == month:
            b = daily[d.day]
            b["revenue"] += r["revenue"]
            if r.get("profit") is None:
                b["uncertain_count"] += 1
            else:
                b["profit"] += r["profit"]
            b["order_count"] += 1
    return [
        {"day": d, "date": f"{year:04d}-{month:02d}-{d:02d}", **daily[d]}
        for d in range(1, days_in_month + 1)
    ]


def _demo() -> None:
    """실행 가능한 자체 점검 — 매칭 우선순위와 미상(None) 처리만 검증 (파일 IO 없음)."""
    from dataclasses import dataclass

    @dataclass
    class FakeOrder:
        product_id: str
        product_name: str

    registered = [{"name": "실리콘주걱", "naver_product_id": "999", "supply_price": 1000}]

    p, m = _match_registered(FakeOrder("999", "이름이바뀐상품"), registered)
    assert m == "id" and p["name"] == "실리콘주걱", "ID 매칭 우선순위 실패"

    p, m = _match_registered(FakeOrder("", "실리콘주걱"), registered)
    assert m == "name", "이름 폴백 실패"

    p, m = _match_registered(FakeOrder("", "전혀다른상품"), registered)
    assert m == "none" and p == {}, "미매칭 판정 실패"

    records = [
        {"date": "2026-08-01", "revenue": 1000, "profit": 200},
        {"date": "2026-08-01", "revenue": 500, "profit": None},  # 원가미상
    ]
    series = month_series(records, 2026, 8)
    day1 = series[0]
    assert day1["revenue"] == 1500 and day1["profit"] == 200 and day1["uncertain_count"] == 1, \
        "미상 주문이 순수익에 섞이거나 매출에서 빠짐"

    print("sales self-check OK")


if __name__ == "__main__":
    _demo()
