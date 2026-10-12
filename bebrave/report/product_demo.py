"""상품 관리 샘플 데이터 — 요약 칸(조치 필요·마진 부족·60일+ 무판매·판매중지·품절·임박)마다
다른 상품이 걸리도록 만든 가상 상품. 판정은 하지 않는다 — 실데이터와 같은 판정 함수가 그대로 탄다.

각 상품이 보여주려는 상황
  정상 판매          캠핑 접이식 테이블
  도매처 품절         미니 랜턴 USB            → 바로 조치(판매중지 반영)
  품절 임박           폴딩 체어 커버           → 지켜보기
  도매가 상승         차량용 컵홀더 확장형     → 바로 조치(권장가 적용)
  수량 불일치         방수 지퍼백 대용량       → 바로 조치(수량 맞추기)
  잘 팔리던 상품 중지  실리콘 냄비받침          → 바로 조치(판매 재개)
  남는 돈 적음+82일 0건 3단 자동우산             → 교체 권장
  70일 0건(마진은 충분) 욕실 미끄럼방지 매트     → 교체 권장(이름 다시 짓기)
  팔리지만 남는 돈 적음 휴대용 선풍기 거치대    → 교체 권장 + 가격 변경 효과 이력
  막 등록             여행용 목베개            → 지켜보기
"""
from datetime import date, timedelta


def demo_products(today: date = None):
    """(행 목록, 주문 기록, 조치 이력, 발굴 후보) — 행은 판정 전 원재료 형태."""
    from ..margin.calculator import calculate
    today = today or date.today()

    def row(pid, name, keyword, price, cost, days, sold_30, sold_total, suspended=False, sync=None,
            auto_delete=False, months_since_sold=None):
        m = calculate(price, cost, free_shipping=price >= 30_000)
        sync = dict(sync, naver_product_id=pid) if sync else None
        filters = []
        if suspended:
            filters.append("suspended")
        if sync and sync.get("action") in ("판매중지", "재고조정"):
            filters.append("stock")
        if sync and sync.get("action") == "마진경고":
            filters.append("margin")
        return {
            "naver_product_id": pid, "name": name, "keyword": keyword,
            "sale_price": price, "supply_price": cost, "margin_rate": round(m.margin_rate, 4),
            "is_suspended": suspended, "sale_status": "판매중지" if suspended else "판매중",
            "sync": sync, "recent_order_count": sold_30, "order_count": sold_total,
            "revenue": sold_total * price, "days_since_registered": days,
            "registered_date": (today - timedelta(days=days)).isoformat(),
            "perf_status": "판매중" if sold_30 else ("신규(관찰중)" if days < 14 else "무판매"),
            "replacements": None, "auto_delete_risk": auto_delete, "months_since_sold": months_since_sold,
            "filters": filters,
        }

    ok_stock = {"action": "정상", "supply_stock": 230}
    rows = [
        row("D1", "캠핑 접이식 테이블 경량 알루미늄 롤테이블", "캠핑테이블", 18900, 9800, 120, 12, 41, sync=ok_stock),
        row("D2", "미니 랜턴 USB 충전식 캠핑 무드등", "캠핑랜턴", 15900, 6900, 95, 3, 18,
            sync={"action": "판매중지", "supply_stock": 0, "detail": "도매처 품절"}),
        row("D3", "폴딩 체어 커버 방수 캠핑의자 커버", "캠핑의자커버", 12900, 5400, 64, 4, 9,
            sync={"action": "정상", "supply_stock": 6}),
        row("D4", "차량용 컵홀더 확장형 대용량 텀블러 거치", "차량컵홀더", 11900, 7900, 88, 2, 11,
            sync={"action": "마진경고", "supply_stock": 140, "suggested_price": 14900,
                  "detail": "도매가 6,100 → 7,900원"}),
        row("D5", "방수 지퍼백 대용량 여행 파우치 3종", "방수파우치", 13900, 5200, 71, 1, 6,
            sync={"action": "재고조정", "supply_stock": 40, "new_stock": 40, "detail": "등록 수량 999 → 40"}),
        row("D6", "실리콘 냄비받침 내열 주방 트리벳 2P", "실리콘냄비받침", 12900, 4800, 150, 0, 37, suspended=True,
            sync=ok_stock),
        row("D7", "3단 자동우산 우양산 경량 접이식", "자동우산", 4600, 3190, 82, 0, 0, suspended=True,
            sync={"action": "정상", "supply_stock": 500}),
        row("D8", "욕실 미끄럼방지 매트 규조토 발매트", "욕실매트", 16900, 7200, 70, 0, 0,
            sync={"action": "정상", "supply_stock": 320}),
        row("D9", "휴대용 선풍기 거치대 유모차 집게형", "선풍기거치대", 9900, 5400, 110, 6, 22,
            sync={"action": "정상", "supply_stock": 85}),
        row("D10", "여행용 목베개 메모리폼 기내용", "여행용목베개", 14700, 6100, 9, 0, 0,
            sync={"action": "정상", "supply_stock": 210}),
    ]

    # 주문 기록 — D9는 12일 전 가격을 올린 뒤 주문이 늘어난 모습(조치 효과 이력 시연)
    d = lambda n: (today - timedelta(days=n)).isoformat()
    sales = ([{"naver_product_id": "D9", "date": d(n), "revenue": 8900} for n in (20, 16)] +
             [{"naver_product_id": "D9", "date": d(n), "revenue": 9900} for n in (11, 9, 7, 5, 3, 1)])
    actions = [
        {"naver_product_id": "D9", "kind": "price", "at": d(12), "summary": "판매가 8,900 → 9,900원"},
        {"naver_product_id": "D6", "kind": "suspend", "at": d(3), "summary": "도매처 확인 차 판매중지"},
    ]
    candidates = [
        {"keyword": "캠핑 사이드 테이블", "category": "캠핑용품", "score": 78, "supply_goods_no": "S1", "track": "A"},
        {"keyword": "차박 수납 오거나이저", "category": "차량용품", "score": 72, "supply_goods_no": "S2", "track": "A"},
        {"keyword": "규조토 욕실매트 대형", "category": "욕실용품", "score": 69, "supply_goods_no": "S3", "track": "A"},
    ]
    return rows, sales, actions, candidates
