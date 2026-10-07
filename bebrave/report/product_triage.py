"""상품 관리 표의 "문제 · 할 일" 판정 — 상품 1개의 상태를 보고 무엇이 문제이고 무엇을 누르면 되는지 정한다.

표가 상태 배지만 늘어놓고 "그래서 이 상품을 어떻게 하라는 건지"를 말하지 않았다(2026-10).
판정은 여기 한 곳에서만 하고, 화면은 결과(문제 문장 + 버튼)를 그대로 그린다.

행동 종류
  post — 상품 관리 일괄처리(products_bulk)에 이 상품 1건만 실어 보낸다(네이버에 실제 반영, 확인 창 거침)
  link — 다른 화면으로 이동(교체 후보 미리보기 등)
  modal — 상품 상세 창을 연다(판매가 직접 변경 등)
"""
from typing import Optional

# 판정 등급 — 표 정렬과 배지 색에 같이 쓴다
URGENT, REPLACE, WATCH, OK = "urgent", "replace", "watch", "ok"
VERDICT_LABEL = {URGENT: "바로 조치", REPLACE: "교체 권장", WATCH: "지켜보기", OK: "정상"}
VERDICT_ORDER = {URGENT: 0, REPLACE: 1, WATCH: 2, OK: 3}


def triage(p: dict, min_abs_profit: int, new_days: int, dead_days: int) -> dict:
    """p: products_view가 만든 행(dict). 반환: verdict, issues[{level,text}], actions[...]"""
    issues, actions = [], []
    eligible = set(p.get("eligible") or [])
    sync = p.get("sync") or {}
    sync_action = sync.get("action", "")
    stock = sync.get("supply_stock")
    unit_profit = round((p.get("sale_price") or 0) * (p.get("margin_rate") or 0))
    days = p.get("days_since_registered")
    sold_30 = p.get("recent_order_count") or 0
    sold_total = p.get("order_count") or 0
    urgent = replace = watch = False

    # 1) 팔리면 바로 손해·취소가 나는 것 — 도매처 품절·수량 불일치·원가 상승
    if (stock is not None and stock <= 0) or sync_action == "판매중지":
        urgent = True
        issues.append({"level": "bad", "text": "도매처 품절 — 주문이 들어와도 발주할 수 없습니다"})
        if "apply_sync" in eligible:
            actions.append({"kind": "post", "action": "apply_sync", "label": "판매중지 반영"})
    elif stock is not None and stock <= 10:
        watch = True
        issues.append({"level": "warn", "text": f"도매처 재고 {stock}개 — 품절 임박"})
    if sync_action == "재고조정":
        urgent = True
        issues.append({"level": "warn", "text": "도매처 수량과 내 등록 수량이 다릅니다"})
        if "apply_sync" in eligible:
            actions.append({"kind": "post", "action": "apply_sync", "label": "수량 맞추기"})
    if sync_action == "마진경고":
        urgent = True
        sp = sync.get("suggested_price")
        issues.append({"level": "bad", "text": "도매가가 올라 마진이 줄었습니다" + (f" — 권장가 {sp:,}원" if sp else "")})
        if "apply_price" in eligible:
            actions.append({"kind": "post", "action": "apply_price", "label": "권장가 적용"})
    if sync_action == "확인실패":
        watch = True
        issues.append({"level": "warn", "text": "도매처 정보를 확인하지 못했습니다"})

    # 2) 남겨둘 이유가 없는 것 — 팔려도 남는 돈이 적거나, 오래 안 팔림
    low_profit = unit_profit < min_abs_profit
    if low_profit:
        issues.append({"level": "bad", "text": f"개당 {unit_profit:,}원 남음 — 기준 {min_abs_profit:,}원 미달"})
    # 정리 기준은 헬스체크 "정리 대상(60일+ 0건)"과 같은 숫자를 쓴다 — 두 화면의 개수가 달라지지 않게
    no_sale_long = days is not None and days >= dead_days and sold_30 == 0
    if no_sale_long:
        issues.append({"level": "bad", "text": f"등록 {days}일째 " + ("판매 0건" if not sold_total else "최근 30일 판매 0건")})
    elif days is not None and new_days <= days < dead_days and sold_30 == 0:
        watch = True
        issues.append({"level": "warn", "text": f"등록 {days}일 — 아직 판매 없음 ({dead_days}일 넘으면 교체 대상)"})
    if p.get("auto_delete_risk"):
        issues.append({"level": "bad", "text": f"{p.get('months_since_sold')}개월 미판매 — 네이버 자동삭제 대상"})
    if low_profit or no_sale_long:
        replace = True
        rep = (p.get("replacements") or [None])[0]
        if rep:
            actions.append({"kind": "link", "label": f"교체 후보: {rep.get('keyword', '')} {rep.get('score', 0)}점",
                            "url": rep.get("url", "/candidates")})
        else:
            actions.append({"kind": "link", "label": "교체 후보 찾기", "url": "/candidates"})
        if low_profit and p.get("detail_url"):
            # 교체 말고 "가격을 올려 살리기"도 선택지 — 상세 창의 판매가 칸에서 순이익을 보며 바꾼다
            actions.append({"kind": "modal", "label": "가격 바꿔 보기", "url": p["detail_url"]})
        if no_sale_long and not low_profit and "reoptimize" in eligible:
            actions.append({"kind": "post", "action": "reoptimize", "label": "이름 다시 짓기"})
        if not p.get("is_suspended"):
            actions.append({"kind": "post", "action": "suspend", "label": "판매중지"})

    # 3) 네이버에서 판매중지 — 교체 대상이 아니면 다시 여는 게 할 일
    if p.get("is_suspended"):
        issues.append({"level": "warn", "text": "네이버에서 판매중지 상태입니다"})
        if not replace and "resume" in eligible:
            urgent = True
            actions.append({"kind": "post", "action": "resume", "label": "판매 재개"})

    if not issues and days is not None and days < new_days:
        watch = True
        issues.append({"level": "info", "text": f"등록 {days}일 — 아직 판단하기 이릅니다"})

    verdict = URGENT if urgent else REPLACE if replace else WATCH if watch else OK
    return {"verdict": verdict, "verdict_label": VERDICT_LABEL[verdict], "issues": issues,
            "actions": actions[:3], "unit_profit": unit_profit, "low_profit": low_profit}


def _demo() -> None:
    """실행 가능한 자체 점검 — 대표 상황 4개의 판정 (파일·네트워크 없음)."""
    base = {"sale_price": 20000, "margin_rate": 0.3, "days_since_registered": 40, "recent_order_count": 3,
            "order_count": 5, "eligible": ["suspend", "apply_sync", "apply_price", "reoptimize", "resume"]}
    kw = dict(min_abs_profit=5000, new_days=14, dead_days=60)
    assert triage(base, **kw)["verdict"] == OK
    # 개당 944원 + 82일 무판매 + 판매중지 → 교체 권장, 재개 버튼은 없어야 한다(되살릴 상품이 아님)
    r = triage(dict(base, sale_price=4600, margin_rate=0.205, days_since_registered=82, recent_order_count=0,
                    order_count=0, is_suspended=True), **kw)
    assert r["verdict"] == REPLACE and r["unit_profit"] == 943, r
    assert not any(a.get("action") == "resume" for a in r["actions"]), r["actions"]
    # 도매처 품절 → 바로 조치 + 판매중지 반영 버튼
    r = triage(dict(base, sync={"action": "판매중지", "supply_stock": 0}), **kw)
    assert r["verdict"] == URGENT and r["actions"][0]["action"] == "apply_sync", r
    # 막 등록한 상품 → 지켜보기
    assert triage(dict(base, days_since_registered=5, recent_order_count=0, order_count=0), **kw)["verdict"] == WATCH
    # 30일째 무판매(마진은 충분) → 아직 교체 아님, 지켜보기
    assert triage(dict(base, days_since_registered=30, recent_order_count=0, order_count=0), **kw)["verdict"] == WATCH
    print("product_triage self-check OK")


if __name__ == "__main__":
    _demo()
