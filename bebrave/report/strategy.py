"""
스토어 전략 진단 — 헬스체크(+옛 주간리포트)의 두뇌.

질문은 하나다: "그래서 이번 달에 나는 뭘 해야 하나." 숫자를 보여주는 데서 끝내지 않고
세 축(성장·운영·돈)을 판정한 뒤, 우선순위 규칙으로 전략 하나와 할 일 최대 3개를 낸다.

구조는 두 단계로 나눴다.
  collect_metrics()  원장에서 지표를 모은다(파일 읽기만, 네트워크 없음). 모르는 값은 None.
  diagnose(metrics)  지표 → 판정·할 일. 순수 함수라 샘플 데이터도 같은 규칙을 탄다 —
                     샘플 화면에서 확인한 판정이 실데이터에서도 그대로 나온다.

원칙
  - 처방 우선순위는 운영 → 돈 → 성장으로 고정. 지연·취소가 늘고 있을 때 상품을 늘리면
    페널티도 같이 커지고, 돈이 막혀 있으면 확장할 수가 없다.
  - 모르면 모른다고 한다. 표본이 적으면 판정 대신 "보류" — 틀린 처방보다 보류가 낫다.
  - 할 일은 "동사 + 대상 + 개수 + 바로가기"로만. "고려해보세요" 같은 문장은 쓰지 않는다.
  - 판정은 규칙으로 낸다(같은 데이터면 같은 답, 근거 추적 가능).
"""
import calendar
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import (
    GOOD_SERVICE_MIN, FAST_SETTLEMENT_MAX_RETURN, FAST_SETTLEMENT_MIN_ORDERS,
    AUTO_DELETE_MONTHS, FREE_SHIPPING_THRESHOLD,
)

MANUAL_PATH = Path("data/health_manual.json")        # 직접 입력값(굿서비스 점수, 월 순수익 목표)
HISTORY_PATH = Path("data/strategy_history.json")    # 달마다 내린 처방과 그때의 지표

# ── 판정 기준 ───────────────────────────────────────────────────────────────
# 네이버 페널티·굿서비스 세부 기준은 정책이 바뀐다 — 아래 수치는 "위험 신호" 용도의
# 보수적 내부 기준이지 네이버 공식 수치가 아니다(공식 기준은 판매자센터에서 확인 필요).
MIN_SAMPLE_ORDERS = 5          # 최근 60일 주문이 이보다 적으면 성장 축 판정 보류
DEAD_DAYS = 60                 # 등록 후 이 기간 넘게 0건이면 정리 대상
NEW_DAYS = 14                  # 등록 후 이 기간은 판단 유예
SELL_RATE_DROP = 0.8           # 판매 발생률이 평소의 80% 밑이면 정리기
DEAD_RATIO_LIMIT = 0.3         # 판매 중 상품의 30% 이상이 정리 대상이면 정리기
MARGIN_DROP_PP = 0.03          # 순수익률이 3%p 이상 빠졌는데 매출은 늘면 수익성 점검
CONCENTRATION_LIMIT = 0.5      # 1등 상품이 매출 절반 이상이면 집중 위험
DELAY_WARN, DELAY_RISK = 0.03, 0.10          # 발송 지연율
STOCKOUT_RISK = 0.05                          # 품절 취소율
SELLER_FAULT_WARN = 2                         # 판매자 귀책 반품 건수(30일)
FEE_DIFF_WARN = 0.01                          # 실측 수수료가 가정보다 1%p 이상 높으면
CASH_CYCLE_WARN = 14                          # 발주→정산 입금 평균 일수

LEVEL_ORDER = {"risk": 0, "warn": 1, "ok": 2, "hold": 3}
LEVEL_LABEL = {"risk": "위험", "warn": "주의", "ok": "양호", "hold": "보류"}

# 네이버 클레임 사유 코드 중 판매자 귀책으로 보는 것 + 한글 사유 키워드(필드 실주문 미검증 — claims.py 참고)
SELLER_FAULT_CODES = {"SOLD_OUT", "DELAYED_DELIVERY", "DROPPED_DELIVERY", "BROKEN",
                      "INCORRECT_INFO", "WRONG_DELIVERY", "WRONG_OPTION"}
SELLER_FAULT_WORDS = ("품절", "지연", "누락", "파손", "불량", "상이", "오배송", "잘못")
STOCKOUT_WORDS = ("품절", "SOLD_OUT", "재고")


def _pct(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.0%}"


def _ch(now, prev) -> Optional[float]:
    if now is None or not prev:
        return None
    return (now - prev) / prev


# ── 직접 입력값 / 처방 이력 ────────────────────────────────────────────────

def load_manual() -> dict:
    if not MANUAL_PATH.exists():
        return {}
    return json.loads(MANUAL_PATH.read_text(encoding="utf-8"))


def save_manual(**fields) -> None:
    data = load_manual()
    for k, v in fields.items():
        if v is None:
            continue
        data[k] = {"value": v, "updated_at": date.today().isoformat()}
    MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history() -> list:
    if not HISTORY_PATH.exists():
        return []
    return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))


def _save_history(history: list) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot(m: dict) -> dict:
    """처방 결과를 나중에 비교할 핵심 지표만 남긴다."""
    g, o, mo = m.get("growth", {}), m.get("ops", {}), m.get("money", {})
    return {
        "sell_rate": g.get("sell_rate"), "dead_count": (g.get("lifecycle") or {}).get("dead"),
        "revenue_30": g.get("revenue_30"), "margin_rate": g.get("margin_rate"),
        "delay_rate": o.get("delay_rate"), "return_rate": o.get("return_rate"),
        "fee_diff": (mo.get("fee") or {}).get("diff"),
    }


# 전략별로 "효과를 어떤 숫자로 볼지" — 지난 처방 결과 칸이 이걸로 전후를 비교한다.
RESULT_METRIC = {
    "cleanup": ("sell_rate", "판매 발생률", "pct"),
    "expand": ("revenue_30", "30일 매출", "won"),
    "margin": ("margin_rate", "순수익률", "pct"),
    "decline": ("revenue_30", "30일 매출", "won"),
    "ops": ("delay_rate", "발송 지연율", "pct"),
    "cash": ("fee_diff", "수수료 차이", "pp"),
    "keep": ("revenue_30", "30일 매출", "won"),
}


def record_prescription(result: dict, metrics: dict, today: Optional[date] = None) -> None:
    """이번 달 처방을 이력에 남긴다. 보류 판정은 남기지 않는다(처방이 아니므로).
    같은 달 안에서는 실행 여부를 기록하기 전까지 처방 내용을 최신 판정으로 갱신한다 —
    잘못 입력한 굿서비스 점수 하나로 생긴 판정이 한 달 내내 박제되면 안 된다.
    단 비교 기준(snapshot)은 그 달 첫 진단 값을 유지한다("처방 전" 상태여야 하므로)."""
    today = today or date.today()
    if result["strategy"]["key"] == "hold":
        return
    month = today.strftime("%Y-%m")
    history = load_history()
    cur = next((h for h in history if h["month"] == month), None)
    if cur is not None:
        if cur.get("done") is None:
            cur.update(key=result["strategy"]["key"], title=result["strategy"]["title"],
                       actions=[a["text"] for a in result["actions"]])
            _save_history(history)
        return
    history.append({
        "month": month, "key": result["strategy"]["key"], "title": result["strategy"]["title"],
        "actions": [a["text"] for a in result["actions"]], "done": None,
        "snapshot": _snapshot(metrics),
    })
    _save_history(history)


def mark_done(month: str, done: bool) -> None:
    history = load_history()
    for h in history:
        if h["month"] == month:
            h["done"] = done
    _save_history(history)


def last_result(history: list, metrics: dict, today: Optional[date] = None) -> Optional[dict]:
    """지난달(이번 달 이전 가장 최근) 처방과, 그 처방이 겨냥한 지표의 그때 vs 지금."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    past = [h for h in history if h["month"] < month]
    if not past:
        return None
    h = past[-1]
    key, label, unit = RESULT_METRIC.get(h["key"], RESULT_METRIC["keep"])
    before = (h.get("snapshot") or {}).get(key)
    after = _snapshot(metrics).get(key)

    def fmt(v):
        if v is None:
            return "—"
        if unit == "won":
            return f"{v:,.0f}원"
        if unit == "pp":
            return f"{v * 100:+.1f}%p"
        return f"{v:.0%}"

    better = None
    if before is not None and after is not None and before != after:
        lower_is_better = key in ("delay_rate", "return_rate", "fee_diff", "dead_count")
        better = (after < before) if lower_is_better else (after > before)
    return {**h, "metric_label": label, "before": fmt(before), "after": fmt(after), "better": better}


# ── 판정 ───────────────────────────────────────────────────────────────────
# 화면 위계에 맞춰 축마다 세 가지를 낸다.
#   headline  문제를 평이한 한 문장으로(숫자 나열 금지 — 무엇이 문제인지 읽어서 찾게 하지 않는다)
#   chips     핵심 숫자 2~4개. 문제인 칩만 색을 입혀 눈이 바로 거기로 가게 한다.
#   actions   할 일 — title(굵게: 무엇을)과 why(흐리게: 왜)로 나눈다.

def _act(title: str, why: str, link: str, link_label: str = "바로가기") -> dict:
    return {"title": title, "why": why, "link": link, "link_label": link_label}


def _chip(label: str, value: str, level: str = "ok", sub: str = "") -> dict:
    return {"label": label, "value": value, "level": level, "sub": sub}


def _growth(g: dict) -> dict:
    """성장 축 — 상품과 매출. 전략 유형(정리기/확장기/수익성 점검/하락/유지)을 여기서 정한다."""
    if not g or (g.get("orders_60") or 0) < MIN_SAMPLE_ORDERS:
        n = (g or {}).get("orders_60") or 0
        return {"level": "hold", "kind": "hold", "headline": "주문이 적어 아직 경향을 말하기 이릅니다",
                "chips": [_chip("최근 60일 주문", f"{n}건", "hold", f"{MIN_SAMPLE_ORDERS}건부터 판정")], "actions": []}

    active, sell_rate, base = g.get("active_products") or 0, g.get("sell_rate"), g.get("sell_rate_base")
    lc = g.get("lifecycle") or {}
    dead = lc.get("dead", 0)
    dead_ratio = dead / active if active else 0
    rev_d, m_now, m_prev = g.get("revenue_delta"), g.get("margin_rate"), g.get("margin_rate_prev")
    prod_ch = _ch(active, g.get("active_products_prev"))

    rate_low = bool(base and sell_rate is not None and sell_rate < base * SELL_RATE_DROP)
    chips = [
        _chip("팔린 상품 비율", _pct(sell_rate), "warn" if rate_low else "ok",
              f"평소 {_pct(base)}" if base else "최근 30일"),
        _chip("30일 매출", _pct(rev_d, True), "warn" if rev_d is not None and rev_d <= -0.15 else "ok", "직전 30일 대비"),
        _chip("정리 대상", f"{dead}개", "warn" if dead_ratio >= DEAD_RATIO_LIMIT else "ok", f"{DEAD_DAYS}일+ 0건"),
    ]

    actions = []
    if rate_low or dead_ratio >= DEAD_RATIO_LIMIT:
        kind, level = "cleanup", "warn"
        headline = ("상품은 늘었는데 팔리는 비율이 떨어졌습니다" if prod_ch and prod_ch >= 0.05
                    else "안 팔리는 상품이 쌓여 스토어 효율을 깎고 있습니다")
        if dead:
            actions.append(_act(f"정리 대상 {dead}개 판매중지", f"{DEAD_DAYS}일 넘게 한 건도 안 팔린 상품입니다",
                                "/products?tab=nosale", "목록 보기"))
            actions.append(_act(f"빈자리에 교체 후보 {min(dead, 5)}개 등록", "같은 상품 수로 팔리는 비율을 끌어올립니다",
                                "/candidates", "교체 후보"))
    elif rev_d is not None and rev_d > 0.05 and m_now is not None and m_prev is not None and m_prev - m_now >= MARGIN_DROP_PP:
        kind, level = "margin", "warn"
        headline = "매출은 늘었지만 남는 돈의 비율이 줄었습니다"
        chips[2] = _chip("순수익률", _pct(m_now), "warn", f"직전 {_pct(m_prev)}")
        low = g.get("low_margin_sellers") or 0
        actions.append(_act(f"저마진 판매 상품 {low}개 가격 1~2천원 인상" if low else "판매 상위 상품 가격 인상 테스트",
                            "팔리는데 건당 남는 돈이 기준보다 적습니다", "/products", "상품 보기"))
    elif rev_d is not None and rev_d <= -0.15:
        kind, level = "decline", "warn"
        headline = "매출이 크게 줄었습니다"
        falling = lc.get("falling", 0)
        actions.append(_act(f"판매가 꺾인 상품 {falling}개 확인" if falling else "매출 빠진 상품 확인",
                            "경쟁 상품 유입·가격·품절 여부부터 봅니다", "/products", "상품 보기"))
    elif sell_rate is not None and (base is None or sell_rate >= base) and (rev_d is None or rev_d >= 0):
        kind, level = "expand", "ok"
        headline = "팔리는 비율과 매출이 함께 오르고 있습니다"
        best = g.get("best_segment")
        actions.append(_act(f"{best} 후보 3~5개 추가 등록" if best else "잘 팔리는 상품과 비슷한 후보 3~5개 등록",
                            "지금 잘 되는 쪽에 상품을 더하는 게 성공 확률이 가장 높습니다", "/candidates", "발굴 후보"))
    else:
        kind, level = "keep", "ok"
        headline = "큰 변화 없이 유지되고 있습니다"

    top1 = g.get("top1")
    if top1 and top1.get("share", 0) >= CONCENTRATION_LIMIT:
        if level == "ok":
            level = "warn"
        chips.append(_chip("1등 상품 비중", _pct(top1["share"]), "warn", top1["name"][:10]))
        actions.append(_act(f"'{top1['name'][:12]}' 대체 도매처·연관상품 확보",
                            "매출 절반이 한 상품에 몰려 있어, 품절되면 매출이 한 번에 빠집니다", "/products", "상품 보기"))

    story = growth_story(g, kind)
    if kind == "cleanup" and story["culprit"] == "new" and len(actions) >= 2:
        actions[1] = _act(f"빈자리는 점수 높은 후보로만 {min(dead, 5)}개 교체",
                          f"최근 올린 상품 판매율이 {_pct(story['new_rate'])}로 기존({_pct(story['old_rate'])})보다 낮습니다 — 같은 기준으로 고르면 반복됩니다",
                          "/candidates", "발굴 후보")
    return {"level": level, "kind": kind, "headline": headline, "chips": chips, "actions": actions, "story": story}


def _ops(o: dict, manual: dict) -> dict:
    """운영 축 — 배송·취소·반품·굿서비스. 건이 아니라 비율로만 본다(건 처리는 홈 몫)."""
    gs = (manual.get("good_service") or {})
    gs_score = gs.get("value")
    gs_stale = True
    if gs.get("updated_at"):
        try:
            gs_stale = (date.today() - date.fromisoformat(gs["updated_at"])).days > 30
        except ValueError:
            pass
    manual_prompt = "굿서비스 점수를 입력하면 판정에 포함됩니다" if gs_score is None else (
        "굿서비스 점수 입력이 30일 지났습니다" if gs_stale else None)
    gs_ok = gs_score is not None and not gs_stale
    gs_chip = _chip("굿서비스", f"{gs_score}점" if gs_ok else "미입력",
                    ("risk" if gs_score < GOOD_SERVICE_MIN else "ok") if gs_ok else "hold", f"기준 {GOOD_SERVICE_MIN}점")
    gs_act = _act(f"굿서비스 {gs_score}점 → {GOOD_SERVICE_MIN}점 회복",
                  "점수가 기준 아래면 노출 혜택을 잃습니다 — 발송·문의 응답 속도부터",
                  "/orders", "주문·발주") if gs_ok and gs_score < GOOD_SERVICE_MIN else None

    if not o or (o.get("orders_30") or 0) < MIN_SAMPLE_ORDERS:
        # 주문 2건 중 1건 지연 = 50%처럼 표본이 작으면 비율이 과장된다. 굿서비스 미달만은 표본과 무관하게 본다.
        n = (o or {}).get("orders_30") or 0
        if gs_act:
            return {"level": "risk", "headline": "굿서비스 점수가 기준 아래입니다", "chips": [gs_chip],
                    "actions": [gs_act], "manual_prompt": manual_prompt}
        return {"level": "hold", "headline": "주문이 적어 비율을 내기 이릅니다",
                "chips": [_chip("최근 30일 주문", f"{n}건", "hold", f"{MIN_SAMPLE_ORDERS}건부터 판정"), gs_chip],
                "actions": [], "manual_prompt": manual_prompt}

    delay, stockout = o.get("delay_rate"), o.get("stockout_rate") or 0
    fault, rr = o.get("seller_fault_returns") or 0, o.get("return_rate")
    d_lv = "hold" if delay is None else ("risk" if delay >= DELAY_RISK else ("warn" if delay >= DELAY_WARN else "ok"))
    s_lv = "risk" if stockout >= STOCKOUT_RISK else ("warn" if stockout else "ok")
    f_lv = "warn" if fault >= SELLER_FAULT_WARN else "ok"
    chips = [
        _chip("발송 지연율", _pct(delay), d_lv, "24시간 초과"),
        _chip("품절 취소", f"{o.get('stockout_count', 0)}건", s_lv, "최근 30일"),
        _chip("반품률", _pct(rr), f_lv, f"판매자 귀책 {fault}건"),
        gs_chip,
    ]
    actions, problems = [], []
    if d_lv in ("risk", "warn"):
        problems.append((d_lv, "발송 지연이 " + ("위험 수준입니다" if d_lv == "risk" else "늘고 있습니다")))
        actions.append(_act(f"발송 지연 {o.get('delay_count', 0)}건의 병목 해소",
                            f"주로 {o.get('delay_bottleneck') or '발주·송장 단계'}에서 막힙니다 — 지연은 페널티로 이어집니다",
                            "/orders?tab=dispatch", "주문·발주"))
    if s_lv in ("risk", "warn"):
        problems.append((s_lv, "도매 품절로 결제 후 취소가 나고 있습니다"))
        actions.append(_act(f"품절 취소 상품 {o.get('stockout_count', 0)}개 재고 확인", "같은 도매처에서 반복되면 소싱에서 제외합니다",
                            "/products?tab=action", "상품 관리"))
    if f_lv == "warn":
        problems.append(("warn", "반품 중 판매자 책임 건이 늘고 있습니다"))
        actions.append(_act(f"귀책 반품 상품 {fault}건 상세페이지 수정", "실물·옵션 정보와 상세페이지가 다르다는 신호입니다",
                            "/cs", "CS 보기"))
    if gs_act:
        problems.append(("risk", "굿서비스 점수가 기준 아래입니다"))
        actions.append(gs_act)

    if problems:
        problems.sort(key=lambda p: LEVEL_ORDER[p[0]])
        level, headline = problems[0]
    else:
        level, headline = "ok", "배송·취소·반품 모두 안정적입니다"
    return {"level": level, "headline": headline, "chips": chips, "actions": actions, "manual_prompt": manual_prompt}


def _money(mo: dict) -> dict:
    """돈 축 — 마진이 실제로 남는지, 돈이 제때 들어오는지, 확장할 여력이 있는지."""
    if not mo or mo.get("revenue_30") in (None, 0):
        return {"level": "hold", "headline": "최근 30일 매출이 없어 돈 흐름을 판정할 수 없습니다", "chips": [], "actions": []}

    cap, weekly, fee = mo.get("capacity_orders"), mo.get("weekly_orders"), mo.get("fee")
    un, cyc, fs = mo.get("unsettled_count"), mo.get("cash_cycle_days"), mo.get("fast_settlement_ok")
    chips, actions, problems = [], [], []

    if cap is not None:
        lv = "risk" if weekly and cap < weekly else "ok"
        chips.append(_chip("발주 여력", f"{cap}건", lv, f"주 평균 주문 {weekly}건"))
        if lv == "risk":
            problems.append(("risk", "지금 잔액으로는 한 주 주문도 발주하지 못합니다"))
            actions.append(_act(f"이머니 {mo.get('cash_shortfall', 0):,}원 확보",
                                f"한 주 평균 주문 {weekly}건을 발주하기에 부족합니다 — 팔려도 못 보내면 취소·페널티",
                                "/orders?tab=ready", "주문·발주"))
    if fee:
        lv = "warn" if fee["diff"] >= FEE_DIFF_WARN else "ok"
        chips.append(_chip("실측 수수료", f"{fee['measured_rate']:.1%}", lv, f"가정 {fee['assumed_rate']:.1%}"))
        if lv == "warn":
            problems.append(("warn", "실제 수수료가 예상보다 높아 마진이 부풀려 계산되고 있습니다"))
            actions.append(_act(f"마진 계산 수수료 {fee['measured_rate']:.1%}로 갱신",
                                f"지금은 {fee['assumed_rate']:.1%}로 계산해 상품마다 남는 돈이 실제보다 크게 잡힙니다",
                                "/settlement?tab=reconcile", "정산 대사"))
    if cyc is not None:
        lv = "warn" if cyc >= CASH_CYCLE_WARN else "ok"
        chips.append(_chip("현금 회전", f"{cyc:.0f}일", lv, "주문 → 정산 입금"))
        if lv == "warn":
            problems.append(("warn", "발주에 쓴 돈이 돌아오기까지 오래 걸립니다"))
    if un:
        chips.append(_chip("정산 미입금", f"{un}건", "warn", f"{mo.get('unsettled_amount', 0):,}원"))
        problems.append(("warn", "정산이 안 들어온 주문이 있습니다"))
        actions.append(_act(f"정산 안 들어온 {un}건 확인",
                            f"{mo.get('unsettled_amount', 0):,}원 — 주문 20일이 지났는데 정산 기록이 없습니다",
                            "/settlement?tab=reconcile", "정산 대사"))
    if fs is False:
        problems.append(("warn", "빠른정산 자격을 잃었습니다"))
        actions.append(_act("빠른정산 자격 회복",
                            f"반품률 {FAST_SETTLEMENT_MAX_RETURN:.0%} 미만·월 {FAST_SETTLEMENT_MIN_ORDERS}건 이상이면 정산이 빨라집니다",
                            "/cs", "CS 보기"))

    if not chips:
        return {"level": "hold", "headline": "수수료·정산 지표가 아직 없습니다 — 정산 화면에서 동기화하면 판정합니다",
                "chips": [], "actions": []}
    if problems:
        problems.sort(key=lambda p: LEVEL_ORDER[p[0]])
        level, headline = problems[0]
    else:
        level, headline = "ok", "마진·정산 흐름이 정상입니다"
    return {"level": level, "headline": headline, "chips": chips, "actions": actions}


STRATEGY_TEXT = {
    "ops": ("운영 안정화", "주문을 늘리기 전에 배송·취소부터 바로잡을 때입니다"),
    "cash": ("현금 확보", "확장보다 돈이 돌게 만드는 게 먼저입니다"),
    "cleanup": ("정리기", "새로 등록하기보다 안 팔리는 상품 교체가 먼저입니다"),
    "expand": ("확장기", "잘 되는 쪽으로 상품을 늘릴 때입니다"),
    "margin": ("수익성 점검", "가격과 원가부터 손볼 때입니다"),
    "decline": ("하락 진단", "무엇이 빠졌는지부터 확인할 때입니다"),
    "keep": ("유지", "지금 하던 대로 등록과 관리를 이어가면 됩니다"),
    "hold": ("판단 보류", "주문이 더 쌓이면 전략을 판정합니다"),
}


def diagnose(m: dict, manual: Optional[dict] = None) -> dict:
    """지표 → 이번 달 전략 + 할 일(최대 3) + 축별 진단(headline·chips)."""
    manual = manual if manual is not None else {}
    axes = {
        "growth": {"name": "성장", "sub": "상품·매출", **_growth(m.get("growth") or {})},
        "ops": {"name": "운영", "sub": "배송·취소·반품", **_ops(m.get("ops") or {}, manual)},
        "money": {"name": "돈", "sub": "마진·정산·현금", **_money(m.get("money") or {})},
    }

    # 우선순위: 운영 위험 → 돈 위험 → 성장 전략
    if axes["ops"]["level"] == "risk":
        key, primary = "ops", "ops"
    elif axes["money"]["level"] == "risk":
        key, primary = "cash", "money"
    elif axes["growth"]["level"] == "hold":
        key, primary = "hold", None   # 성장은 보류여도 운영·돈에 할 일이 있으면 그건 낸다
    else:
        key, primary = axes["growth"]["kind"], "growth"

    # 할 일: 주 축 먼저, 나머지는 위험 → 주의 순으로 채워 최대 3개
    order = ([primary] if primary else []) + sorted(
        [k for k in axes if k != primary and axes[k]["level"] in ("risk", "warn")],
        key=lambda k: LEVEL_ORDER[axes[k]["level"]])
    actions = []
    for k in order:
        for a in axes[k]["actions"]:
            if len(actions) >= 3:
                break
            actions.append({**a, "axis": k, "n": len(actions) + 1, "text": a["title"]})
    for k, ax in axes.items():
        ax["refs"] = [a["n"] for a in actions if a["axis"] == k]
        ax["level_label"] = LEVEL_LABEL[ax["level"]]
        ax["key"] = k

    if key == "hold" and actions:
        title, summary = "판단 보류", "매출 판정은 이르지만, 운영·돈 쪽에 먼저 손볼 것이 있습니다"
    else:
        title, summary = STRATEGY_TEXT[key]
    return {
        "strategy": {"key": key, "title": title, "summary": summary,
                     "why": axes[primary]["headline"] if primary else None,
                     "axis": primary, "level": axes[primary]["level"] if primary else "hold"},
        "actions": actions,
        "axes": [axes["growth"], axes["ops"], axes["money"]],
    }


# ── 실데이터 지표 수집 ──────────────────────────────────────────────────────

def week_summary(weeks: list) -> Optional[dict]:
    """완료된 주 기준 최근 4주 vs 그 전 4주 주평균. 진행 중인 이번 주는 빼야 꺾인 것처럼 안 보인다."""
    done = [w["revenue"] for w in weeks if not w.get("partial")]
    if len(done) < 8:
        return None
    recent, before = sum(done[-4:]) / 4, sum(done[-8:-4]) / 4
    return {"recent_avg": round(recent), "prev_avg": round(before), "change": _ch(recent, before)}


def week_chart(weeks: list) -> Optional[dict]:
    """꺾은선 좌표(0~1)와 눈금. 템플릿에서 기하 계산을 하지 않게 여기서 끝낸다."""
    if not weeks:
        return None
    peak = max(w["revenue"] for w in weeks) or 1
    # 최고점의 10~15% 위에서 가장 가까운 깔끔한 값(1·1.2·1.5·2·2.5·3·4·5·6·8 × 10^n) — 축이 너무 크면 선이 바닥에 눌린다
    target, mag = peak * 1.1, 10 ** (len(str(int(peak * 1.1))) - 1)
    top = next(f * mag for f in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if f * mag >= target)
    n = len(weeks)
    pts = [{"x": (i + 0.5) / n, "y": w["revenue"] / top, "value": w["revenue"], "label": w["label"],
            "partial": bool(w.get("partial")), "orders": w.get("orders", 0)} for i, w in enumerate(weeks)]
    ws = week_summary(weeks)
    done = [i for i, w in enumerate(weeks) if not w.get("partial")]
    bands = None
    if ws and len(done) >= 8:
        bands = {"prev": {"x0": done[-8] / n, "x1": (done[-5] + 1) / n, "y": ws["prev_avg"] / top, "value": ws["prev_avg"]},
                 "recent": {"x0": done[-4] / n, "x1": (done[-1] + 1) / n, "y": ws["recent_avg"] / top, "value": ws["recent_avg"]}}
    ticks = [{"y": f, "value": round(top * f)} for f in (0, 0.5, 1)]
    return {"points": pts, "bands": bands, "ticks": ticks}


def _won_short(v) -> str:
    return f"{v / 10000:,.1f}만원" if v >= 10000 else f"{v:,.0f}원"


def growth_story(g: dict, kind: str) -> dict:
    """성장 축을 펼쳤을 때의 세 단계 문장. 숫자는 그래프·목록이 보여주고, 문장은 결론만 말한다."""
    ws = g.get("week_summary")
    flow = None
    if ws:
        c = ws["change"] or 0
        verb = "줄었습니다" if c < -0.05 else ("늘었습니다" if c > 0.05 else "비슷합니다")
        flow = (f"최근 4주 주평균 {_won_short(ws['recent_avg'])} — 그 전 4주({_won_short(ws['prev_avg'])})보다 "
                f"{abs(c):.0%} {verb}" if verb != "비슷합니다" else
                f"최근 4주 주평균 {_won_short(ws['recent_avg'])} — 그 전 4주와 비슷합니다")

    falling = (ws and (ws["change"] or 0) < 0) or kind in ("cleanup", "decline")
    movers = g.get("losers" if falling else "winners") or []
    co = g.get("cohort") or {}
    new, old = co.get("new") or {}, co.get("old") or {}
    new_rate = new["sold"] / new["count"] if new.get("count") else None
    old_rate = old["sold"] / old["count"] if old.get("count") else None
    culprit = None
    if new_rate is not None and old_rate is not None and new.get("count", 0) >= 3:
        culprit = "new" if new_rate < old_rate * 0.6 else ("old" if old_rate < new_rate * 0.6 else "both")

    if kind == "margin":
        low = g.get("low_margin_sellers") or 0
        cause = (f"순수익률이 {_pct(g.get('margin_rate_prev'))}에서 {_pct(g.get('margin_rate'))}로 내려왔습니다 — "
                 + (f"팔리는 상품 중 {low}개가 건당 남는 돈이 기준보다 적습니다" if low else "많이 팔리는 상품의 마진이 얇습니다"))
        fix = "그 상품들의 가격을 1~2천원 올려 판매가 유지되는지 2주간 보세요"
        return {"flow": flow, "movers": g.get("winners") or [], "movers_falling": False, "cause": cause, "fix": fix,
                "new_rate": new_rate, "old_rate": old_rate, "culprit": None, "out": [], "dead_total": 0, "in": []}
    if culprit == "new":
        cause = "새로 올린 상품이 처음부터 잘 안 팔립니다 — 같은 방식으로 고른 후보로 교체하면 같은 결과가 반복됩니다"
        fix = "점수 높은 후보만 골라 교체하고, 발굴 기준을 한 단계 올리세요"
    elif culprit == "old":
        cause = "예전에 팔리던 상품들이 식고 있습니다 — 수명이 다한 상품을 새 상품으로 바꿀 때입니다"
        fix = "식은 상품을 빼고 그 자리에 새 후보를 넣으세요"
    else:
        cause = "특정 상품군이 아니라 전반적으로 덜 팔립니다" if falling else "잘 되는 상품이 매출을 끌어올리고 있습니다"
        fix = "안 팔리는 상품을 빼고 점수 높은 후보로 채우세요" if falling else "잘 되는 상품과 비슷한 후보를 더 올리세요"
    return {"flow": flow, "movers": movers, "movers_falling": bool(falling), "cause": cause, "fix": fix,
            "new_rate": new_rate, "old_rate": old_rate, "culprit": culprit,
            "out": (g.get("dead_list") or [])[:5] if kind in ("cleanup", "decline", "keep") else [],
            "dead_total": (g.get("lifecycle") or {}).get("dead", 0),
            "in": g.get("replacements") or []}


def week_trend(weeks: list) -> Optional[float]:
    """완료된 주 기준 최근 4주 평균 vs 그 전 4주 평균. 진행 중인 이번 주는 빼야 꺾인 것처럼 안 보인다."""
    done = [w["revenue"] for w in weeks if not w.get("partial")]
    if len(done) < 8:
        return None
    recent, before = sum(done[-4:]) / 4, sum(done[-8:-4]) / 4
    return _ch(recent, before)


def _load_json(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _goal_block(manual: dict, sales: list, today: date, orders_30: int, active: int, sold: int) -> Optional[dict]:
    """월 순수익 목표 역산 — 목표 ÷ 건당 순수익 = 필요 주문 수 → 지금 페이스와의 차이를
    "상품 몇 개 더" 또는 "판매율 몇 %p"로 바꿔 말한다. 목표를 안 넣었으면 None."""
    target = (manual.get("profit_goal") or {}).get("value")
    if not target:
        return None
    month_start = today.replace(day=1)
    rows = [r for r in sales if (_d(r.get("date")) or date.min) >= month_start and r.get("profit") is not None]
    profit = sum(r["profit"] for r in rows)
    dim = calendar.monthrange(today.year, today.month)[1]
    pace = round(profit / today.day * dim) if today.day else 0
    per_order = (profit / len(rows)) if rows else None
    gap = max(0, target - pace)
    need_orders = round(gap / per_order) if per_order and gap else 0
    per_product = (orders_30 / active) if active else None   # 판매 중 상품 1개가 한 달에 만드는 주문
    need_products = round(need_orders / per_product) if per_product and need_orders else None
    return {"target": target, "profit": profit, "pace": pace, "progress": min(1, pace / target) if target else 0,
            "gap": gap, "need_orders": need_orders, "need_products": need_products,
            "per_order": round(per_order) if per_order else None}


def collect_metrics(today: Optional[date] = None) -> dict:
    """원장 → 지표. 원장이 없거나 비어 있으면 해당 값은 None/0으로 남고 diagnose가 보류로 처리한다."""
    from .sales import load_orders
    from .claims import load_claims, return_rate
    from .reconcile import reconcile, suggest_fee_rate
    from .settlement_ledger import load_settlements
    from ..margin.calculator import calculate as calc_margin
    from ..smartstore.purchase_queue import load_queue, STATUS_DISPATCHED

    today = today or date.today()
    manual = load_manual()
    sales = load_orders()
    registered = _load_json("data/registered_products.json")
    status_cache = _load_json("data/product_status_cache.json")
    suspended = {s.get("product_id") for s in status_cache if s.get("status_type") == "SUSPENSION"}
    reg_by_id = {str(p.get("naver_product_id", "")): p for p in registered}

    def window(start_back: int, end_back: int) -> list:
        lo, hi = today - timedelta(days=start_back - 1), today - timedelta(days=end_back)
        return [r for r in sales if (d := _d(r.get("date"))) and lo <= d <= hi]

    s30, s60 = window(30, 0), window(60, 30)

    def active_at(back: int) -> list:
        cut = today - timedelta(days=back)
        return [p for p in registered
                if (_d(p.get("registered_date")) or date.min) <= cut and str(p.get("naver_product_id")) not in suspended]

    def sell_rate(rows: list, back: int):
        act = active_at(back)
        ids = {str(p.get("naver_product_id")) for p in act}
        sold = {r.get("naver_product_id") for r in rows if r.get("naver_product_id") in ids}
        return (len(sold) / len(act) if act else None), len(act), len(sold)

    rate_now, active_now, sold_now = sell_rate(s30, 0)
    rate_prev, active_prev, _ = sell_rate(s60, 30)
    base_rates = [r for r in (sell_rate(window(30 * (k + 1), 30 * k), 30 * k)[0] for k in (1, 2, 3)) if r is not None]

    rev30, rev60 = sum(r["revenue"] for r in s30), sum(r["revenue"] for r in s60)

    def margin(rows):
        known = [r for r in rows if r.get("profit") is not None]
        rv = sum(r["revenue"] for r in known)
        return (sum(r["profit"] for r in known) / rv) if rv else None

    # 12주 추이
    weeks = []
    monday = today - timedelta(days=today.weekday())
    for k in range(11, -1, -1):
        ws = monday - timedelta(weeks=k)
        rows = [r for r in sales if (d := _d(r.get("date"))) and ws <= d < ws + timedelta(days=7)]
        weeks.append({"label": ws.strftime("%m/%d"), "revenue": sum(r["revenue"] for r in rows),
                      "profit": sum(r["profit"] for r in rows if r.get("profit") is not None), "orders": len(rows),
                      "partial": k == 0 and today.weekday() != 6})

    # 상품 생애주기
    cnt30, cnt_prev, cnt60, total = {}, {}, {}, {}
    for r in sales:
        pid, d = r.get("naver_product_id"), _d(r.get("date"))
        if not pid or not d:
            continue
        total[pid] = total.get(pid, 0) + 1
        age = (today - d).days
        if age < 30:
            cnt30[pid] = cnt30.get(pid, 0) + 1
        elif age < 60:
            cnt_prev[pid] = cnt_prev.get(pid, 0) + 1
        if age < DEAD_DAYS:
            cnt60[pid] = cnt60.get(pid, 0) + 1
    lc = {"new": 0, "star": 0, "falling": 0, "dead": 0, "steady": 0}
    dead_list, auto_delete_risk = [], 0
    for p in active_at(0):
        pid = str(p.get("naver_product_id"))
        days = (today - (_d(p.get("registered_date")) or today)).days
        n, pv = cnt30.get(pid, 0), cnt_prev.get(pid, 0)
        if days < NEW_DAYS:
            lc["new"] += 1
        elif days >= DEAD_DAYS and not cnt60.get(pid):
            lc["dead"] += 1
            dead_list.append({"name": p.get("name", ""), "days": days, "total": total.get(pid, 0)})
            if days >= AUTO_DELETE_MONTHS * 30 - 60:
                auto_delete_risk += 1
        elif pv >= 2 and n <= pv * 0.5:
            lc["falling"] += 1
        elif n >= 2 and n >= pv:
            lc["star"] += 1
        else:
            lc["steady"] += 1
    dead_list.sort(key=lambda x: -x["days"])

    # 매출 집중도(90일)
    by_pid = {}
    for r in window(90, 0):
        by_pid[r.get("naver_product_id") or "미매칭"] = by_pid.get(r.get("naver_product_id") or "미매칭", 0) + r["revenue"]
    tot90 = sum(by_pid.values())
    top1 = None
    if tot90:
        pid, v = max(by_pid.items(), key=lambda kv: kv[1])
        top1 = {"name": reg_by_id.get(pid, {}).get("name", pid), "share": v / tot90}

    # 팔리는데 남는 게 적은 상품
    low_margin = 0
    for pid in cnt30:
        p = reg_by_id.get(pid)
        if p and not calc_margin(sale_price=p.get("sale_price", 0), cost_price=p.get("supply_price", 0),
                                 free_shipping=p.get("sale_price", 0) >= FREE_SHIPPING_THRESHOLD).passes_abs_floor:
            low_margin += 1

    # 상품별 매출 증감 — 최근 28일 vs 직전 28일(그래프의 "최근 4주"와 같은 단위)
    mv = {}
    for r in sales:
        d, pid = _d(r.get("date")), r.get("naver_product_id")
        if not d or not pid:
            continue
        age = (today - d).days
        if age < 28:
            mv.setdefault(pid, [0, 0])[1] += r["revenue"]
        elif age < 56:
            mv.setdefault(pid, [0, 0])[0] += r["revenue"]
    moves = [{"name": reg_by_id.get(pid, {}).get("name", pid), "before": b, "after": a, "delta": a - b}
             for pid, (b, a) in mv.items() if a != b]
    losers = sorted([x for x in moves if x["delta"] < 0], key=lambda x: x["delta"])[:5]
    winners = sorted([x for x in moves if x["delta"] > 0], key=lambda x: -x["delta"])[:5]

    # 신규(등록 14~60일) vs 기존(60일+) 상품의 최근 30일 판매율 — 판매율 하락이 "새 상품이 약해서"인지
    # "기존 상품이 식어서"인지 가른다. 처방이 다르다(발굴 기준 올리기 vs 교체).
    cohort = {"new": {"count": 0, "sold": 0}, "old": {"count": 0, "sold": 0}}
    for p in active_at(0):
        days = (today - (_d(p.get("registered_date")) or today)).days
        if days < NEW_DAYS:
            continue
        c = cohort["new" if days < DEAD_DAYS else "old"]
        c["count"] += 1
        c["sold"] += 1 if cnt30.get(str(p.get("naver_product_id"))) else 0

    # 교체 후보 — 발굴 후보 중 진입 권장 점수 이상·미등록, 점수순(replacement.py와 같은 기준)
    from .replacement import suggest_replacements
    replacements = [{"keyword": c.get("keyword", ""), "category": c.get("category", ""), "score": c.get("score", 0),
                     "price": c.get("est_sale_price"), "cost": c.get("est_cost_price")}
                    for c in suggest_replacements("", _load_json("data/sourcing_log.json"), registered, limit=5)]

    growth = {
        "losers": losers, "winners": winners, "cohort": cohort, "replacements": replacements,
        "orders_60": len(s30) + len(s60), "revenue_30": rev30, "revenue_prev": rev60,
        "revenue_delta": _ch(rev30, rev60), "profit_30": sum(r["profit"] for r in s30 if r.get("profit") is not None),
        "active_products": active_now, "active_products_prev": active_prev,
        "sold_products": sold_now, "sell_rate": rate_now, "sell_rate_prev": rate_prev,
        "sell_rate_base": (sum(base_rates) / len(base_rates)) if base_rates else None,
        "rev_per_product": (rev30 / active_now) if active_now else None,
        "rev_per_product_prev": (rev60 / active_prev) if active_prev else None,
        "margin_rate": margin(s30), "margin_rate_prev": margin(s60),
        "lifecycle": lc, "dead_list": dead_list[:20], "auto_delete_risk": auto_delete_risk,
        "top1": top1, "low_margin_sellers": low_margin, "weeks": weeks, "week_trend": week_trend(weeks),
        "week_summary": week_summary(weeks), "week_chart": week_chart(weeks),
        "best_segment": None,
        "goal": _goal_block(manual, sales, today, len(s30), active_now, sold_now),
    }

    # 운영
    queue = load_queue()
    q30 = [i for i in queue if (d := _d(i.get("ordered_at"))) and (today - d).days < 30]
    delayed = []
    for i in q30:
        try:
            ordered = datetime.fromisoformat(i.get("ordered_at", ""))
        except ValueError:
            continue
        if i.get("status") == STATUS_DISPATCHED:
            done = _d(i.get("updated_at"))   # 발송처리 시각이 따로 없어 갱신일로 근사(하루 단위)
            late = done is not None and (done - ordered.date()).days >= 2
        else:
            late = (datetime.now() - ordered).total_seconds() >= 24 * 3600
        if late:
            delayed.append(i)
    bottleneck, delay_stage = None, None
    if delayed:
        stuck = sum(1 for i in delayed if i.get("status") in ("ready", "hold"))
        bottleneck = "발주 단계(발주 대기·보류 방치)" if stuck * 2 >= len(delayed) else "송장 단계(도매처 출고)"
        delay_stage = {"발주 단계": stuck, "송장 단계": len(delayed) - stuck}
    claims30 = [c for c in load_claims() if (d := _d(c.get("claimed_at"))) and (today - d).days < 30]
    stockouts = [c for c in claims30 if any(w in f"{c.get('claim_reason', '')}" for w in STOCKOUT_WORDS)]
    fault = [c for c in claims30 if c.get("claim_type", "").upper().startswith(("RETURN", "EXCHANGE"))
             and (c.get("claim_reason") in SELLER_FAULT_CODES
                  or any(w in f"{c.get('claim_reason', '')}" for w in SELLER_FAULT_WORDS))]
    rr = return_rate(days=30)
    sync_cache = _load_json("data/product_sync_cache.json")
    ops = {
        "orders_30": len(s30),
        "delay_count": len(delayed), "delay_rate": (len(delayed) / len(q30)) if q30 else None,
        "delay_bottleneck": bottleneck, "delay_stage": delay_stage,
        "stockout_count": len(stockouts), "stockout_rate": (len(stockouts) / len(s30)) if s30 else None,
        "return_rate": rr.get("rate"), "return_count": rr.get("claim_count"), "seller_fault_returns": len(fault),
        "soldout_products": len([s for s in sync_cache if s.get("action") == "판매중지"]),
        "good_service": (manual.get("good_service") or {}).get("value"),
    }

    # 돈
    recon = reconcile()
    fee = suggest_fee_rate(recon)
    cycles = [(sd - od).days for r in recon if (od := _d(r.get("date"))) and (sd := _d(r.get("settle_date")))]
    settled_ids = {r["product_order_id"] for r in recon}
    unsettled = []
    if load_settlements():   # 정산 원장을 한 번도 동기화 안 했으면 "누락"이 아니라 "모름"
        unsettled = [r for r in sales if (d := _d(r.get("date"))) and 20 <= (today - d).days <= 90
                     and r["product_order_id"] not in settled_ids]
    # 매출 → 순수익 폭포(30일) — 등록 시 마진 계산 기준 추정. 원가 매칭 안 된 주문은 제외.
    wf = {"revenue": 0, "cost": 0, "fee": 0, "shipping": 0, "reserve": 0, "profit": 0}
    for r in s30:
        p = reg_by_id.get(r.get("naver_product_id") or "")
        if not p or not p.get("sale_price"):
            continue
        qty = max(1, round(r["revenue"] / p["sale_price"]))
        mr = calc_margin(sale_price=p["sale_price"], cost_price=p.get("supply_price", 0),
                         free_shipping=p["sale_price"] >= FREE_SHIPPING_THRESHOLD)
        wf["revenue"] += mr.sale_price * qty
        wf["cost"] += mr.cost_price * qty
        wf["fee"] += (mr.order_fee + mr.sales_fee) * qty
        wf["shipping"] += mr.shipping_cost * qty
        wf["reserve"] += mr.cs_reserve * qty
        wf["profit"] += mr.net_profit * qty
    money = {
        "revenue_30": rev30, "fee": fee,
        "cash_cycle_days": (sum(cycles) / len(cycles)) if cycles else None,
        "unsettled_count": len(unsettled), "unsettled_amount": sum(r["revenue"] for r in unsettled),
        "capacity_orders": None, "weekly_orders": round(len(s30) / 30 * 7) if s30 else 0,
        "fast_settlement_ok": None if rr.get("rate") is None else (
            rr["rate"] < FAST_SETTLEMENT_MAX_RETURN and len(s30) >= FAST_SETTLEMENT_MIN_ORDERS),
        "waterfall": wf if wf["revenue"] else None,
    }
    return {"growth": growth, "ops": ops, "money": money}


# ── 샘플 ───────────────────────────────────────────────────────────────────

DEMO_SCENARIOS = {
    "cleanup": "정리기", "expand": "확장기", "margin": "수익성 점검",
    "ops": "운영 안정화", "cash": "현금 확보", "hold": "판단 보류",
}


def demo_metrics(scenario: str = "cleanup", today: Optional[date] = None) -> tuple:
    """샘플 지표 + 샘플 직접입력 + 샘플 이력. 시나리오마다 판정이 달라지게 숫자만 비튼다."""
    today = today or date.today()
    weeks_rev = [62000, 71000, 58000, 83000, 96000, 88000, 104000, 91000, 79000, 72000, 68000, 61000]
    monday = today - timedelta(days=today.weekday())
    weeks = [{"label": (monday - timedelta(weeks=11 - i)).strftime("%m/%d"), "revenue": v,
              "profit": round(v * 0.17), "orders": max(1, v // 9000), "partial": i == 11} for i, v in enumerate(weeks_rev)]
    weeks[-1]["revenue"], weeks[-1]["profit"] = 24000, 4100   # 이번 주는 아직 진행 중
    g = {
        "orders_60": 58, "revenue_30": 281000, "revenue_prev": 342000, "revenue_delta": -0.18,
        "profit_30": 46400, "active_products": 42, "active_products_prev": 32,
        "sold_products": 9, "sell_rate": 0.21, "sell_rate_prev": 0.31, "sell_rate_base": 0.34,
        "rev_per_product": 6690, "rev_per_product_prev": 10690,
        "margin_rate": 0.165, "margin_rate_prev": 0.172,
        "lifecycle": {"new": 6, "star": 4, "falling": 3, "dead": 14, "steady": 15},
        "dead_list": [{"name": n, "days": d, "total": t} for n, d, t in [
            ("스테인리스 텀블러 500ml", 128, 0), ("차량용 컵홀더 확장형", 112, 1), ("욕실 미끄럼방지 매트", 97, 0),
            ("접이식 빨래건조대 미니", 88, 0), ("방수 지퍼백 대용량", 81, 2), ("실리콘 냄비받침 3P", 74, 0)]],
        "auto_delete_risk": 0,
        "top1": {"name": "캠핑 접이식 테이블", "share": 0.34}, "low_margin_sellers": 3,
        "weeks": weeks, "week_trend": week_trend(weeks), "best_segment": "캠핑용품 1~2만원대",
        "week_summary": week_summary(weeks), "week_chart": week_chart(weeks),
        "losers": [{"name": n, "before": b, "after": a, "delta": a - b} for n, b, a in [
            ("캠핑 접이식 테이블", 98000, 61000), ("차량용 컵홀더 확장형", 32000, 9000),
            ("방수 지퍼백 대용량", 27000, 11000), ("실리콘 주방집게", 18000, 6000)]],
        "winners": [{"name": n, "before": b, "after": a, "delta": a - b} for n, b, a in [
            ("캠핑 접이식 테이블", 61000, 98000), ("미니 랜턴 USB", 12000, 31000), ("폴딩 체어 커버", 4000, 15000)]],
        "cohort": {"new": {"count": 16, "sold": 2}, "old": {"count": 20, "sold": 7}},
        "replacements": [
            {"keyword": "캠핑 사이드 테이블", "category": "캠핑용품", "score": 78, "price": 18900, "cost": 9800},
            {"keyword": "차박 수납 오거나이저", "category": "차량용품", "score": 72, "price": 15900, "cost": 7400},
            {"keyword": "접이식 실리콘 버킷", "category": "캠핑용품", "score": 69, "price": 12900, "cost": 5600},
            {"keyword": "휴대용 선풍기 거치대", "category": "생활잡화", "score": 64, "price": 9900, "cost": 4100},
            {"keyword": "방수 파우치 3종", "category": "여행용품", "score": 61, "price": 8900, "cost": 3200}],
        "goal": {"target": 500000, "profit": 4600, "pace": 138000, "progress": 0.28, "gap": 362000,
                 "need_orders": 76, "need_products": 23, "per_order": 4760},
    }
    o = {"orders_30": 31, "delay_count": 1, "delay_rate": 0.02, "delay_bottleneck": "송장 단계(도매처 출고)",
         "delay_stage": {"발주 단계": 0, "송장 단계": 1},
         "stockout_count": 0, "stockout_rate": 0.0, "return_rate": 0.06, "return_count": 2,
         "seller_fault_returns": 1, "soldout_products": 2, "good_service": 4.7}
    mo = {"revenue_30": 281000, "fee": {"measured_rate": 0.052, "assumed_rate": 0.04, "diff": 0.012, "sample_count": 18},
          "cash_cycle_days": 19, "unsettled_count": 0, "unsettled_amount": 0,
          "capacity_orders": 11, "weekly_orders": 7, "cash_shortfall": 0, "fast_settlement_ok": True,
          "waterfall": {"revenue": 281000, "cost": 171400, "fee": 14600, "shipping": 39300, "reserve": 9300, "profit": 46400}}
    manual = {"good_service": {"value": 4.7, "updated_at": today.isoformat()},
              "profit_goal": {"value": 500000, "updated_at": today.isoformat()}}

    if scenario == "expand":
        weeks_up = [dict(w, revenue=v, profit=round(v * 0.17)) for w, v in zip(weeks, [
            58000, 62000, 60000, 69000, 74000, 71000, 82000, 88000, 91000, 99000, 104000, 31000])]
        g.update(weeks=weeks_up, week_trend=week_trend(weeks_up), week_summary=week_summary(weeks_up),
                 week_chart=week_chart(weeks_up), cohort={"new": {"count": 8, "sold": 4}, "old": {"count": 30, "sold": 13}})
        g.update(sell_rate=0.41, sell_rate_base=0.34, revenue_delta=0.22, revenue_30=418000, active_products_prev=38,
                 rev_per_product=9950, rev_per_product_prev=9000, top1={"name": "캠핑 접이식 테이블", "share": 0.28},
                 lifecycle={"new": 6, "star": 11, "falling": 1, "dead": 4, "steady": 20})
        mo.update(fee={"measured_rate": 0.041, "assumed_rate": 0.04, "diff": 0.001, "sample_count": 18}, cash_cycle_days=11)
    elif scenario == "margin":
        g.update(sell_rate=0.36, revenue_delta=0.14, margin_rate=0.118, margin_rate_prev=0.171, active_products_prev=40,
                 lifecycle={"new": 3, "star": 8, "falling": 2, "dead": 6, "steady": 23})
    elif scenario == "ops":
        o.update(delay_count=5, delay_rate=0.16, delay_bottleneck="발주 단계(발주 대기·보류 방치)",
                 delay_stage={"발주 단계": 4, "송장 단계": 1},
                 stockout_count=2, stockout_rate=0.065, seller_fault_returns=3, return_count=4)
    elif scenario == "cash":
        mo.update(capacity_orders=3, weekly_orders=7, cash_shortfall=86000, cash_cycle_days=23)
    elif scenario == "hold":
        g = {"orders_60": 3, "weeks": weeks[:0]}
        o = {"orders_30": 2, "delay_count": 0, "delay_rate": 0.0, "stockout_count": 0, "stockout_rate": 0.0,
             "return_rate": None, "seller_fault_returns": 0}
        mo = {"revenue_30": 18000, "fee": None, "cash_cycle_days": None, "unsettled_count": 0,
              "capacity_orders": None, "fast_settlement_ok": None, "waterfall": None}
        manual = {}

    prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    history = [{"month": prev_month, "key": "cleanup", "title": "정리기",
                "actions": ["60일 넘게 0건인 상품 8개 판매중지", "그 자리에 교체 후보 5개 등록"], "done": True,
                "snapshot": {"sell_rate": 0.18}}]
    return {"growth": g, "ops": o, "money": mo}, manual, history


def _self_check() -> None:
    """규칙 자체 점검 — 우선순위(운영 > 돈 > 성장)와 보류가 의도대로 나오는지."""
    expect = {"cleanup": "cleanup", "expand": "expand", "margin": "margin", "ops": "ops", "cash": "cash", "hold": "hold"}
    for sc, key in expect.items():
        m, manual, _ = demo_metrics(sc)
        r = diagnose(m, manual)
        assert r["strategy"]["key"] == key, f"{sc} 시나리오가 {r['strategy']['key']}로 판정됨"
        assert len(r["actions"]) <= 3, "할 일이 3개를 넘음"
        assert all(a["link"] for a in r["actions"]), "바로가기 없는 할 일"
    # 운영 위험이면 성장이 아무리 좋아도 운영이 1순위
    m, manual, _ = demo_metrics("expand")
    m["ops"].update(delay_rate=0.2, delay_count=6)
    assert diagnose(m, manual)["strategy"]["key"] == "ops", "운영 위험이 성장보다 뒤로 밀림"
    # 할 일 1번은 주 축에서 나온다
    r = diagnose(*demo_metrics("cash")[:2])
    assert r["actions"][0]["axis"] == "money", "현금 확보 전략인데 1번 할 일이 돈 축이 아님"
    # 빈 데이터는 터지지 않고 보류
    r = diagnose({}, {})
    assert r["strategy"]["key"] == "hold" and not r["actions"]
    print("strategy self-check OK")


if __name__ == "__main__":
    _self_check()
