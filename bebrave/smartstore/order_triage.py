"""주문·발주 화면의 상태 판정 — 주문 1건의 단계·경과시간·금액·배송지를 보고 "무엇이 문제이고 무엇을 누르면 되는지".

상품 관리(product_triage)와 같은 원칙: 판정은 여기 한 곳, 화면은 결과만 그린다.

표시 방식(2026-10 개편): 행마다 문장을 읽어야 하던 걸, 2~6글자 "상태 칩"(chip)으로 바꿨다.
긴 설명(tip)은 칩에 마우스를 올리면 보인다. 화면의 다른 칸이 이미 말하는 것(경과 시간, 순이익)은
칩으로 반복하지 않는다. 버튼은 문제에 맞는 것 1~2개만.

시간 기준 — 네이버 발송기한(due_left_h)이 있으면 그걸로, 없으면 결제 후 경과 시간으로
  결제 18시간+ (기한 12시간 이내)  곧 기한(soon)
  결제 24시간+ (기한 지남)         기한 넘음(late)
  결제 48시간+ (기한 하루 넘김)    심각(severe)
"""
SOON_HOURS, LATE_HOURS, SEVERE_HOURS = 18, 24, 48

URGENCY_LABEL = {"severe": "48시간 넘음", "late": "기한 넘음", "soon": "곧 기한", None: ""}

SELLER_CENTER_URL = "https://sell.smartstore.naver.com/"
DOMEME_URL = "https://domeme.domeggook.com/"


def _urgency(hours, due_left=None):
    """네이버 발송기한(due_left: 남은 시간)이 있으면 그걸로, 없으면 결제 후 경과 시간으로."""
    if due_left is not None:
        return "severe" if due_left <= -24 else "late" if due_left <= 0 else "soon" if due_left <= 12 else None
    if hours is None:
        return None
    return "severe" if hours >= SEVERE_HOURS else "late" if hours >= LATE_HOURS else "soon" if hours >= SOON_HOURS else None


def _chip(level, chip, tip):
    return {"level": level, "chip": chip, "tip": tip}


def triage(i: dict, min_abs_profit: int) -> dict:
    """i: 발주 큐 항목(_annotate_orders를 거친 것).
    반환: level, urgency, urgency_label, issues[{level, chip, tip}], actions[{kind, label, ...}], preselect, unit_net"""
    status = i.get("status")
    urgency = _urgency(i.get("hours_since_order"), i.get("due_left_h"))
    issues, actions = [], []
    qty = i.get("quantity") or 1
    margin = i.get("margin_amount")
    unit_net = margin // qty if margin is not None else None
    reason = i.get("hold_reason") or ""
    cost_unknown = status == "ready" and i.get("supply_cost") is None
    goods_url = f"https://domeme.domeggook.com/s/{i['matched_goods_no']}" if i.get("matched_goods_no") else ""
    bad_address = False

    if status == "ready":
        from ..sourcing.domemae_order import delivery_problem
        addr = delivery_problem(i.get("receiver_zipcode", ""), i.get("receiver_address1", ""),
                                i.get("receiver_address2", ""), i.get("receiver_tel", ""))
        if addr:
            bad_address = True
            issues.append(_chip("bad", "배송지 오류", f"{addr} — 이대로 발주하면 도매매가 거절"))
            actions.append({"kind": "edit", "label": "배송지 고치기"})
        if i.get("option_name") and not i.get("matched_option_code"):
            issues.append(_chip("bad", "옵션 미확인", f"고객 옵션 '{i['option_name']}'의 도매 옵션코드를 못 찾음 — 수동 발주"))
            actions.append({"kind": "manual", "label": "수동 발주"})
        if cost_unknown:
            issues.append(_chip("warn", "원가 미확인", "도매가를 몰라 순이익·필요 이머니를 계산 못 함 — 도매매에서 가격 확인 후 발주"))
            if goods_url:
                actions.append({"kind": "link", "label": "도매가 확인", "url": goods_url})
        elif unit_net is not None and unit_net <= 0:
            issues.append(_chip("bad", "손해", f"개당 {unit_net:,}원 — 도매가가 판매가를 넘음. 발주 전 가격 확인"))
        if i.get("match_method") == "name":
            issues.append(_chip("warn", "이름 매칭", "상품번호가 아니라 이름으로 찾은 도매 상품 — 같은 물건인지 확인"))
            if goods_url and not actions:
                actions.append({"kind": "link", "label": "도매 상품 보기", "url": goods_url})

    elif status == "ordered":
        claim = i.get("claim_alert")
        if claim and not i.get("claim_handled"):
            issues.append(_chip("bad", f"{claim.get('label', '취소')} 요청",
                                f"발주 뒤 고객이 {claim.get('label', '취소')} 요청 — 도매처 출고 전이면 도매매 주문을 취소, "
                                "이미 출고됐으면 반품 회수"))
            actions += [{"kind": "link", "label": "도매매 주문 취소", "url": DOMEME_URL},
                        {"kind": "claim_ack", "label": "처리함"}]
        if i.get("naver_confirmed") is False:
            issues.append(_chip("warn", "발주확인 안 됨", "네이버 발주확인이 안 돼 고객이 바로 취소·환불할 수 있음"))
            actions.append({"kind": "naver_confirm", "label": "네이버 발주확인"})
        if urgency in ("late", "severe"):
            issues.append(_chip("bad" if urgency == "severe" else "warn", "출고 지연",
                                "발주는 했는데 도매처가 아직 출고 안 함" +
                                (" — 도매처에 출고 문의, 고객에게 지연 안내" if urgency == "severe" else "")))
            if urgency == "severe":
                actions.append({"kind": "link", "label": "지연 안내(센터)", "url": SELLER_CENTER_URL})
        actions.append({"kind": "sync", "label": "송장 확인"})

    elif status in ("failed", "hold"):
        if "이머니" in reason:
            issues.append(_chip("warn", "이머니 부족", "이머니가 모자라 실패 — 충전 후 다시 발주"))
            actions += [{"kind": "retry", "label": "다시 발주"}, {"kind": "link", "label": "이머니 충전", "url": DOMEME_URL}]
        elif "품절" in reason or "재고" in reason:
            issues.append(_chip("bad", "도매처 품절" if "품절" in reason else "재고 부족",
                                "도매처 재고가 없거나 모자람 — 다른 옵션·도매처로 수동 발주하거나 판매취소"))
            actions += [{"kind": "manual", "label": "수동 발주"},
                        {"kind": "link", "label": "판매취소(센터)", "url": SELLER_CENTER_URL},
                        {"kind": "close", "label": "취소 처리함"}]
        elif "매칭" in reason:
            issues.append(_chip("warn", "매칭 실패", "어떤 도매 상품인지 못 찾음 — 상품번호를 넣어 수동 발주"))
            actions.append({"kind": "manual", "label": "수동 발주"})
        elif any(k in reason for k in ("우편번호", "주소", "휴대전화", "연락처")):
            issues.append(_chip("bad", "배송지 오류", "배송지가 도매매 형식에 맞지 않음 — 고객 확인 후 고쳐서 발주"))
            actions.append({"kind": "manual", "label": "고쳐서 발주"})
        elif "취소" in reason:
            issues.append(_chip("info", "고객 취소", "고객이 취소·반품 요청한 주문 — 발주하지 않음"))
            actions.append({"kind": "close", "label": "확인함"})
        elif status == "failed":
            issues.append(_chip("warn", "일시 오류", "일시 오류일 수 있음 — 다시 시도"))
            actions.append({"kind": "retry", "label": "다시 발주"})
        else:
            actions.append({"kind": "manual", "label": "수동 발주"})

    bad = any(x["level"] == "bad" for x in issues) or urgency in ("late", "severe")
    warn = any(x["level"] == "warn" for x in issues) or urgency == "soon"
    level = "bad" if bad else "warn" if warn else "ok"
    # 일괄 발주에 기본으로 넣을지 — 원가를 모르거나 손해·옵션 미확인·배송지 오류면 사람이 직접 고르게 둔다
    preselect = status == "ready" and not cost_unknown and not (unit_net is not None and unit_net <= 0) \
        and not (i.get("option_name") and not i.get("matched_option_code")) and not bad_address
    return {"level": level, "urgency": urgency, "urgency_label": URGENCY_LABEL[urgency], "issues": issues,
            "actions": actions, "preselect": preselect, "unit_net": unit_net}


def _demo() -> None:
    """실행 가능한 자체 점검 — 대표 상황 판정 (파일·네트워크 없음)."""
    kw = dict(min_abs_profit=5000)
    addr = {"receiver_zipcode": "06000", "receiver_address1": "서울시", "receiver_address2": "101호", "receiver_tel": "01012345678"}
    ok = triage({"status": "ready", "hours_since_order": 3, "quantity": 1, "margin_amount": 7000, "supply_cost": 9000, **addr}, **kw)
    assert ok["level"] == "ok" and ok["preselect"] and not ok["issues"] and not ok["actions"], ok
    # 기한 넘음은 칩이 아니라 경과 시간 칸이 보여준다 — 칩으로 반복하지 않는다
    late = triage({"status": "ready", "hours_since_order": 30, "quantity": 1, "margin_amount": 900, "supply_cost": 3000,
                   "match_method": "name", **addr}, **kw)
    assert late["urgency"] == "late" and late["preselect"] and [x["chip"] for x in late["issues"]] == ["이름 매칭"], late
    unknown = triage({"status": "ready", "hours_since_order": 2, "quantity": 3, "margin_amount": None, "supply_cost": None, **addr}, **kw)
    assert not unknown["preselect"] and unknown["issues"][0]["chip"] == "원가 미확인", unknown
    loss = triage({"status": "ready", "hours_since_order": 2, "quantity": 2, "margin_amount": -800, "supply_cost": 9000, **addr}, **kw)
    assert not loss["preselect"] and loss["unit_net"] == -400 and loss["issues"][0]["chip"] == "손해", loss
    noaddr = triage(dict(addr, status="ready", hours_since_order=2, quantity=1, margin_amount=7000, supply_cost=9000,
                         receiver_address2=""), **kw)
    assert not noaddr["preselect"] and noaddr["actions"][0]["kind"] == "edit", noaddr
    soldout = triage({"status": "failed", "hours_since_order": 10, "hold_reason": "도매처 품절 — 재고 0"}, **kw)
    assert soldout["issues"][0]["chip"] == "도매처 품절", soldout
    # 네이버 발송기한이 주말이라 아직 여유 있으면 30시간이어도 기한 넘음이 아니다
    weekend = triage({"status": "ready", "hours_since_order": 30, "due_left_h": 20, "quantity": 1, "margin_amount": 7000,
                      "supply_cost": 9000, **addr}, **kw)
    assert weekend["urgency"] is None, weekend
    claim = triage({"status": "ordered", "hours_since_order": 10, "claim_alert": {"type": "CANCEL", "label": "취소"},
                    "naver_confirmed": False}, **kw)
    chips = [x["chip"] for x in claim["issues"]]
    assert chips == ["취소 요청", "발주확인 안 됨"] and claim["level"] == "bad", claim
    handled = triage({"status": "ordered", "hours_since_order": 10, "claim_alert": {"type": "CANCEL"}, "claim_handled": True}, **kw)
    assert not handled["issues"], handled
    print("order_triage self-check OK")


if __name__ == "__main__":
    _demo()
