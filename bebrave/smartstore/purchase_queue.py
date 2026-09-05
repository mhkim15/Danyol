"""
발주 대기 큐 — 결제완료 주문을 도매매 발주 후보로 적재하고, 확정 키 매칭 +
도매가/재고 가드로 실행 가능 여부를 판정한다.

이전엔 주문 ↔ 도매매 상품을 상품명 부분일치로만 찾았다 (이름이 조금만 달라도
엉뚱한 상품을 발주할 위험). 이제 스마트스토어 상품ID(naver_product_id)로
우선 매칭하고, 실패할 때만 이름 매칭으로 폴백하되 낮은 신뢰도로 표시한다.

이 모듈은 큐만 만든다 — 실제 발주 실행(돈이 나가는 지점)은 여전히 webapp의
수동 버튼(purchase_place)이 담당한다. 자동 무인 실행은 스케줄러 도입 후 별도.
"""
import json
from datetime import date
from pathlib import Path
from typing import Optional

from ..sourcing.domemae import fetch_product_detail

QUEUE_PATH = Path("data/purchase_queue.json")
REGISTERED_PATH = Path("data/registered_products.json")

PRICE_HIKE_TOLERANCE = 0.10  # 등록 시점 도매가 대비 이 이상 오르면 보류 — 팔수록 손해인 발주를 막기 위함

STATUS_READY = "ready"
STATUS_HOLD = "hold"
STATUS_ORDERED = "ordered"      # 도매매 발주 완료, 송장 미확보
STATUS_DISPATCHED = "dispatched"  # 송장 확보 + 스마트스토어 발송처리까지 완료
STATUS_FAILED = "failed"


def _load_registered() -> list:
    if not REGISTERED_PATH.exists():
        return []
    with open(REGISTERED_PATH, encoding="utf-8") as f:
        return json.load(f)


def match_order_to_product(order, registered: list) -> tuple:
    """(matched_product: dict, method: 'id'|'name'|'none').
    id 매칭이 최우선 — 스마트스토어 상품ID가 같으면 상품명이 달라져도(재작성 등) 확실하다."""
    if order.product_id:
        for p in registered:
            if p.get("naver_product_id") and str(p["naver_product_id"]) == str(order.product_id):
                return p, "id"
    for p in registered:
        name = p.get("name", "")
        if name and (name in order.product_name or order.product_name in name):
            return p, "name"
    return {}, "none"


def _match_option_code(product: dict, option_name: str) -> Optional[str]:
    """주문 옵션명(예: '레드')으로 등록 당시 저장해둔 옵션 목록에서 도매매 옵션코드를 찾는다."""
    if not option_name:
        return None
    for o in product.get("options", []):
        if o.get("name") and o["name"] in option_name:
            return o.get("code")
    return None


def _check_readiness(product: dict, method: str, quantity: int, option_code: Optional[str] = None) -> tuple:
    """(status, reason). method가 id일 때만 도매매 실시간 조회로 가격/재고를 확인한다.

    옵션 상품은 상품 전체 재고가 아니라 해당 옵션의 재고를 봐야 한다 — 전체 재고는
    넉넉해도 특정 옵션(예: 품절 임박 색상)만 부족할 수 있다. 상품 전체 재고만 보고
    ready 처리했다가 특정 옵션 품절로 실제 발주가 실패하는 사고를 막기 위함
    (2026-08 시뮬레이션으로 발견 — 재고 999/특정 옵션 3인데 5개 주문을 ready로 오판정)."""
    if method == "none":
        return STATUS_HOLD, "도매매 상품 매칭 실패 — 수동 확인 필요"
    if method == "name":
        return STATUS_HOLD, "상품ID 매칭 실패, 이름으로만 매칭됨 — 확인 후 발주"

    goods_no = product.get("domemae_goods_no", "")
    if not goods_no:
        return STATUS_HOLD, "등록 정보에 도매매 상품번호 없음"
    try:
        detail = fetch_product_detail(goods_no)
    except Exception as e:
        return STATUS_HOLD, f"도매매 조회 실패: {e}"

    registered_price = product.get("supply_price", 0)
    if registered_price and detail.supply_price > registered_price * (1 + PRICE_HIKE_TOLERANCE):
        hike = detail.supply_price / registered_price - 1
        return STATUS_HOLD, (
            f"도매가 인상 — 등록시 {registered_price:,}원 → 현재 {detail.supply_price:,}원 (+{hike:.0%})"
        )

    if option_code and detail.options:
        opt = next((o for o in detail.options if o.get("code") == option_code), None)
        if opt is None:
            return STATUS_HOLD, f"주문 옵션(코드 {option_code})을 도매매에서 찾을 수 없음 — 옵션 구성이 바뀌었을 수 있음"
        if opt.get("stock", 0) < quantity:
            return STATUS_HOLD, f"도매매 옵션 재고 부족 — '{opt.get('name','')}' 필요 {quantity}개, 재고 {opt.get('stock',0)}개"
        return STATUS_READY, ""

    if detail.stock < quantity:
        return STATUS_HOLD, f"도매매 재고 부족 — 필요 {quantity}개, 재고 {detail.stock}개"
    return STATUS_READY, ""


def load_queue() -> list:
    if not QUEUE_PATH.exists():
        return []
    with open(QUEUE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_queue(items: list) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def build_queue(orders: list, refresh_hold: bool = True) -> list:
    """새 결제완료 주문을 큐에 추가하고, id매칭된 기존 hold 항목은 재검사한다
    (도매가가 다시 내려갔거나 재입고됐을 수 있으니). ordered/failed는 끝난 건이라 건드리지 않는다."""
    registered = _load_registered()
    existing = load_queue()
    by_id = {i["product_order_id"]: i for i in existing}

    for o in orders:
        if o.status != "PAYED":
            continue
        if o.product_order_id in by_id and by_id[o.product_order_id]["status"] in (STATUS_ORDERED, STATUS_FAILED):
            continue

        product, method = match_order_to_product(o, registered)
        option_code = _match_option_code(product, o.option_name) if product else None
        status, reason = _check_readiness(product, method, o.quantity, option_code)

        by_id[o.product_order_id] = {
            "product_order_id": o.product_order_id,
            "order_id": o.order_id,
            "product_name": o.product_name,
            "option_name": o.option_name,
            "quantity": o.quantity,
            "unit_price": o.unit_price,
            "ordered_at": o.ordered_at,
            # 도매처가 고객에게 직배송하므로 배송요청사항이 발주에 실려야 한다 —
            # 지금까지 네이버에서 가져와 놓고 큐에 안 담아 통째로 사라지고 있었다
            # ("부재시 경비실에 맡겨주세요"가 전달 안 돼 배송 실패로 이어짐).
            "delivery_memo": getattr(o, "delivery_memo", ""),
            # CS가 생기면 연락할 사람은 수령인이 아니라 주문자다.
            "orderer_name": getattr(o, "orderer_name", ""),
            "orderer_tel": getattr(o, "orderer_tel", ""),
            "receiver_name": o.receiver_name,
            "receiver_tel": o.receiver_tel,
            "receiver_zipcode": o.receiver_zipcode,
            "receiver_address1": o.receiver_address1,
            "receiver_address2": o.receiver_address2,
            "matched_goods_no": product.get("domemae_goods_no", ""),
            "matched_name": product.get("name", ""),
            "matched_option_code": option_code,
            "match_method": method,
            "status": status,
            "hold_reason": reason,
            "updated_at": date.today().isoformat(),
        }

    if refresh_hold:
        for entry in by_id.values():
            if entry["status"] != STATUS_HOLD or entry["match_method"] != "id" or not entry.get("matched_goods_no"):
                continue
            product = next(
                (p for p in registered if p.get("domemae_goods_no") == entry["matched_goods_no"]), {}
            )
            if not product:
                continue
            status, reason = _check_readiness(product, "id", entry["quantity"], entry.get("matched_option_code"))
            entry["status"], entry["hold_reason"] = status, reason

    items = list(by_id.values())
    _save_queue(items)
    return items


def demote_cancelled(cancelled_order_ids: set) -> list:
    """취소/반품/교환 클레임이 걸린 주문의 발주 대기(ready) 항목을 보류로 강등한다.

    지금까지 큐 빌드는 새로 결제완료(PAYED)된 주문만 봐서, 이미 큐에 들어온 뒤
    취소된 주문은 아무도 다시 안 봐서 그대로 ready로 남아 있었다 — 발주 직전
    재확인(purchase_place/purchase_bulk_place)이 마지막 방어선이지만, 큐를 만드는
    시점에 미리 걸러두면 사람이 발주 버튼을 누르기도 전에 문제를 볼 수 있다.
    이미 발주/발송된 건(ordered/dispatched)은 건드리지 않는다 — 그건 환불/반품
    회수 같은 별도 CS 처리 대상이라 큐 상태를 함부로 못 바꾼다."""
    if not cancelled_order_ids:
        return load_queue()
    items = load_queue()
    changed = False
    for i in items:
        if i["product_order_id"] in cancelled_order_ids and i["status"] in (STATUS_READY, STATUS_HOLD):
            if i["status"] != STATUS_HOLD or i.get("hold_reason") != "주문이 취소됨 — 발주 대상에서 제외 (직접 확인 필요)":
                i["status"] = STATUS_HOLD
                i["hold_reason"] = "주문이 취소됨 — 발주 대상에서 제외 (직접 확인 필요)"
                i["updated_at"] = date.today().isoformat()
                changed = True
    if changed:
        _save_queue(items)
    return items


def refresh_queue(access_token: str, hours: Optional[int] = None) -> list:
    """새 결제완료 주문을 큐에 반영 + 그 사이 취소된 주문을 보류로 강등 — webapp의
    /orders 라우트와 CLI(main.py purchase queue, 주기 실행용)가 같은 로직을
    쓰도록 한 곳에 모았다. 지금까지 이 조합은 웹 라우트 안에만 있어서 사람이
    화면을 열어야만 큐가 갱신됐다(2026-09, 주기 실행 스케줄러 부재 문제)."""
    from ..config import ORDER_QUEUE_WINDOW_HOURS
    from .orders import fetch_new_orders

    window = hours or ORDER_QUEUE_WINDOW_HOURS
    new_orders = fetch_new_orders(access_token, hours=window)
    build_queue(new_orders)
    claims = fetch_new_orders(access_token, hours=window, status_type="CLAIM_REQUESTED")
    cancelled_ids = {c.product_order_id for c in claims if c.claim_type == "CANCEL"}
    return demote_cancelled(cancelled_ids)


def sync_all_tracking(access_token: str) -> list:
    """발주 완료(ordered)건 전체의 도매매 송장을 확인해, 확보되면 스마트스토어
    발송처리까지 실행한다 — webapp의 /purchase/sync_tracking(건별 수동 확인
    버튼)과 같은 일을 전체 건에 자동으로 돈다(주기 실행용, main.py
    purchase sync-tracking). 도매매 로그인은 배치당 한 번만 한다."""
    from ..sourcing.domemae_order import login, fetch_order_tracking
    from .orders import dispatch_order

    ordered = [i for i in load_queue() if i["status"] == STATUS_ORDERED and i.get("domemae_order_no")]
    results = []
    if not ordered:
        return results

    session_data = login()
    for i in ordered:
        try:
            tracking = fetch_order_tracking(i["domemae_order_no"], sId=session_data["sId"])
            if not tracking.get("tracking_number"):
                results.append({"product_order_id": i["product_order_id"], "status": "대기", "detail": "도매매 송장 미등록"})
                continue
            dispatch_order(i["product_order_id"], tracking["tracking_number"],
                            tracking.get("company_name", ""), access_token)
            mark_dispatched(i["product_order_id"], tracking["tracking_number"], tracking.get("company_name", ""))
            results.append({
                "product_order_id": i["product_order_id"], "status": "완료",
                "detail": f"{tracking.get('company_name','')} {tracking['tracking_number']}",
            })
        except Exception as e:
            results.append({"product_order_id": i["product_order_id"], "status": "실패", "detail": str(e)})
    return results


def mark_ordered(product_order_id: str, order_no: str = "", spent_amount: Optional[int] = None) -> None:
    """spent_amount: 실제 지출 추정액(등록 시점 도매가 × 수량). 매칭 안 된 수동발주는
    None(미상) — "나간 돈" 타임라인에서 0으로 잘못 합산되지 않도록 sales.py의
    profit=None과 동일한 관례를 따른다."""
    items = load_queue()
    for i in items:
        if i["product_order_id"] == product_order_id:
            i["status"] = STATUS_ORDERED
            i["domemae_order_no"] = order_no
            i["spent_amount"] = spent_amount
            i["updated_at"] = date.today().isoformat()
    _save_queue(items)


def mark_dispatched(product_order_id: str, tracking_number: str, company: str) -> None:
    items = load_queue()
    for i in items:
        if i["product_order_id"] == product_order_id:
            i["status"] = STATUS_DISPATCHED
            i["tracking_number"] = tracking_number
            i["delivery_company"] = company
            i["updated_at"] = date.today().isoformat()
    _save_queue(items)


def mark_failed(product_order_id: str, reason: str) -> None:
    items = load_queue()
    for i in items:
        if i["product_order_id"] == product_order_id:
            i["status"] = STATUS_FAILED
            i["hold_reason"] = reason
            i["updated_at"] = date.today().isoformat()
    _save_queue(items)


def _demo() -> None:
    """실행 가능한 자체 점검 — 매칭·가드 로직만 검증 (네트워크 호출 없음)."""
    from dataclasses import dataclass

    @dataclass
    class FakeOrder:
        product_order_id: str
        order_id: str
        product_name: str
        product_id: str
        option_name: str
        quantity: int
        unit_price: int
        status: str
        ordered_at: str = ""
        receiver_name: str = ""
        receiver_tel: str = ""
        receiver_zipcode: str = ""
        receiver_address1: str = ""
        receiver_address2: str = ""
        delivery_memo: str = ""
        orderer_name: str = ""
        orderer_tel: str = ""

    registered = [{
        "name": "실리콘주걱", "naver_product_id": "999", "domemae_goods_no": "111",
        "supply_price": 1000, "options": [{"name": "레드", "code": "A1"}],
    }]

    o_id = FakeOrder("po1", "o1", "실리콘주걱 리뉴얼판", "999", "레드", 1, 2000, "PAYED")
    p, m = match_order_to_product(o_id, registered)
    assert m == "id" and p["domemae_goods_no"] == "111", "ID 매칭 실패"
    assert _match_option_code(p, "레드") == "A1", "옵션코드 매칭 실패"

    o_name = FakeOrder("po2", "o2", "실리콘주걱", "", "", 1, 2000, "PAYED")
    p2, m2 = match_order_to_product(o_name, registered)
    assert m2 == "name", "이름 폴백 매칭 실패"

    o_none = FakeOrder("po3", "o3", "전혀다른상품", "", "", 1, 2000, "PAYED")
    p3, m3 = match_order_to_product(o_none, registered)
    assert m3 == "none" and p3 == {}, "미매칭 판정 실패"

    status, reason = _check_readiness({}, "name", 1)
    assert status == STATUS_HOLD and "이름" in reason, "이름매칭 hold 사유 오류"

    # 상품 전체 재고는 넉넉해도 특정 옵션 재고가 부족하면 hold 돼야 한다.
    import types
    from unittest.mock import patch as _patch
    fake_detail = types.SimpleNamespace(
        supply_price=1000, stock=999,
        options=[{"name": "레드", "code": "A1", "stock": 2}, {"name": "블루", "code": "A2", "stock": 50}],
    )
    with _patch(f"{__name__}.fetch_product_detail", return_value=fake_detail):
        status, reason = _check_readiness(
            {"domemae_goods_no": "111", "supply_price": 1000}, "id", quantity=5, option_code="A1")
        assert status == STATUS_HOLD and "옵션 재고" in reason, "옵션별 재고부족을 상품 전체재고로 오판정함"
        status, reason = _check_readiness(
            {"domemae_goods_no": "111", "supply_price": 1000}, "id", quantity=5, option_code="A2")
        assert status == STATUS_READY, "재고 충분한 옵션인데 hold 처리됨"

    import tempfile
    from pathlib import Path as _Path

    # 배송요청사항·주문자가 큐까지 실려야 발주에 태울 수 있다. 도매처가 직배송하므로
    # 여기서 끊기면 고객 요청이 아무 데도 도달하지 않는다(예전에 실제로 끊겨 있었다).
    with tempfile.TemporaryDirectory() as tmp2, _patch(f"{__name__}.QUEUE_PATH", _Path(tmp2) / "q.json"):
        o_memo = FakeOrder("po9", "o9", "실리콘주걱", "999", "", 1, 2000, "PAYED",
                            ordered_at="2026-09-01T10:00", receiver_name="받는이",
                            delivery_memo="부재시 경비실에 맡겨주세요",
                            orderer_name="주문자", orderer_tel="010-0000-1111")
        q = build_queue([o_memo], refresh_hold=False)
        item = next(i for i in q if i["product_order_id"] == "po9")
        assert item["delivery_memo"] == "부재시 경비실에 맡겨주세요", "배송요청사항이 큐에서 사라짐"
        assert item["orderer_name"] == "주문자" and item["orderer_tel"] == "010-0000-1111", \
            "주문자 정보가 큐에서 사라짐"

    # 취소 주문 방어 — 발주 대기(ready) 항목이 취소되면 보류로 강등돼야 한다.
    # 이미 발주된(ordered) 건은 그대로 둔다 — 환불/반품 회수는 별도 CS 처리 대상.
    with tempfile.TemporaryDirectory() as tmp3, _patch(f"{__name__}.QUEUE_PATH", _Path(tmp3) / "q.json"):
        _save_queue([
            {"product_order_id": "po_c1", "status": STATUS_READY, "hold_reason": ""},
            {"product_order_id": "po_c2", "status": STATUS_ORDERED, "hold_reason": ""},
        ])
        demote_cancelled({"po_c1", "po_c2"})
        by_id = {i["product_order_id"]: i for i in load_queue()}
        assert by_id["po_c1"]["status"] == STATUS_HOLD and "취소" in by_id["po_c1"]["hold_reason"], \
            "취소된 발주대기 건이 보류로 안 내려감"
        assert by_id["po_c2"]["status"] == STATUS_ORDERED, "이미 발주된 건을 건드림 — CS 처리 대상은 그대로 둬야 함"

    # 발송처리 마감 — 상태가 dispatched로 넘어가고 택배사가 화면이 읽는 이름으로 저장되는지.
    # (예전엔 저장은 delivery_company인데 화면은 company를 읽어 택배사가 항상 빈칸이었다)
    with tempfile.TemporaryDirectory() as tmp, _patch(f"{__name__}.QUEUE_PATH", _Path(tmp) / "q.json"):
        _save_queue([{"product_order_id": "po1", "status": STATUS_ORDERED}])
        mark_dispatched("po1", "1234567890", "CJ대한통운")
        item = load_queue()[0]
        assert item["status"] == STATUS_DISPATCHED, "발송처리 후에도 상태가 안 바뀜"
        assert item["delivery_company"] == "CJ대한통운", "택배사가 저장되지 않음"
        assert item["tracking_number"] == "1234567890", "송장번호가 저장되지 않음"

    print("purchase_queue self-check OK")


if __name__ == "__main__":
    _demo()
