"""
스토어 헬스체크(거시) — health.py(홈의 "오늘 할 일" 판정용, 개별 문제 나열)와는
다른 질문에 답한다: "전체적으로 매출이 늘고 있나 줄고 있나, 원인 추정은 뭔가,
지금 놓치고 있는 기회는 없나." 전부 이미 로컬에 있는 데이터(sales_orders·
claims·settlements·product_sync_cache·sourcing_log)를 다른 각도로 다시 본다 —
새로 조회하는 값은 없다(발송지연·미답변문의처럼 실시간 조회가 필요한 개별
문제는 health.py 몫으로 남긴다).

정직성 원칙: 표본이 부족하면(주문 5건 미만) 추세를 "판단 불가"로 낸다.
노출·클릭 데이터(비즈어드바이저)가 연동돼 있지 않으므로 "왜 안 팔리는가"를
노출부족/전환부족으로 나누는 건 못 한다 — 답할 수 있는 건 "팔 수 있는
상태였나 / 얼마나 팔렸나"까지다(performance.py와 같은 한계).
"""
from datetime import date, timedelta
from typing import Optional

TREND_WINDOW_DAYS = 28
MIN_TREND_SAMPLE = 5


def _period_stats(records: list, start: date, end: date) -> dict:
    revenue = profit = order_count = uncertain = 0
    for r in records:
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (start <= d <= end):
            continue
        revenue += r["revenue"]
        order_count += 1
        if r.get("profit") is None:
            uncertain += 1
        else:
            profit += r["profit"]
    return {"revenue": revenue, "profit": profit, "order_count": order_count, "uncertain_count": uncertain}


def _delta(this_val: float, prev_val: float) -> Optional[float]:
    if not prev_val:
        return None  # 직전 기간이 0이면 배율이 무의미(분모 0) — 지어내지 않는다
    return (this_val - prev_val) / prev_val


def trend(sales_records: list, today: Optional[date] = None, window_days: int = TREND_WINDOW_DAYS) -> dict:
    """최근 N일 vs 직전 N일 — 반드시 같은 경과일로 잘라서 비교한다(이번달 8일치를
    지난달 31일치와 비교하는 불공정 비교를 막기 위함)."""
    today = today or date.today()
    this_start, this_end = today - timedelta(days=window_days - 1), today
    prev_start, prev_end = this_start - timedelta(days=window_days), this_start - timedelta(days=1)

    this_stats = _period_stats(sales_records, this_start, this_end)
    prev_stats = _period_stats(sales_records, prev_start, prev_end)
    sample = this_stats["order_count"] + prev_stats["order_count"]

    if sample < MIN_TREND_SAMPLE:
        return {"enough_sample": False, "sample_count": sample, "window_days": window_days}

    aov_this = this_stats["revenue"] / this_stats["order_count"] if this_stats["order_count"] else 0
    aov_prev = prev_stats["revenue"] / prev_stats["order_count"] if prev_stats["order_count"] else 0
    return {
        "enough_sample": True, "sample_count": sample, "window_days": window_days,
        "this": this_stats, "prev": prev_stats,
        "revenue_delta": _delta(this_stats["revenue"], prev_stats["revenue"]),
        "profit_delta": _delta(this_stats["profit"], prev_stats["profit"]),
        "order_delta": _delta(this_stats["order_count"], prev_stats["order_count"]),
        "aov_this": aov_this, "aov_prev": aov_prev, "aov_delta": _delta(aov_this, aov_prev),
    }


def _product_contribution(sales_records: list, today: date, window_days: int) -> list:
    """상품별 이번 기간 매출 − 직전 기간 매출. 표본이 있을 때만 의미 있다(trend()가
    이미 표본 부족을 판정하므로 이 함수는 호출 여부만 상위에서 결정)."""
    this_start = today - timedelta(days=window_days - 1)
    prev_start, prev_end = this_start - timedelta(days=window_days), this_start - timedelta(days=1)

    by_product = {}
    for r in sales_records:
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            continue
        pid = r.get("naver_product_id") or "미매칭"
        b = by_product.setdefault(pid, {"this": 0, "prev": 0})
        if this_start <= d <= today:
            b["this"] += r["revenue"]
        elif prev_start <= d <= prev_end:
            b["prev"] += r["revenue"]

    contrib = [{"naver_product_id": pid, "delta": b["this"] - b["prev"]} for pid, b in by_product.items()]
    contrib.sort(key=lambda c: c["delta"], reverse=True)
    return contrib


def causes(registered: list, today: Optional[date] = None) -> list:
    """원인 추정 — 전부 "추정" 표시 + 근거 수치 동반. 순서는 무게순(팔 수 없는
    상태 > 매출 쏠림 > 반품 > 수수료 > 마진)."""
    import json
    from pathlib import Path
    from ..config import FAST_SETTLEMENT_MAX_RETURN
    from .sales import load_orders
    from .claims import return_rate
    from .reconcile import reconcile, suggest_fee_rate
    from ..smartstore.sync import ACTION_MARGIN_WARN

    today = today or date.today()
    out = []
    registered_ids = {str(p.get("naver_product_id", "")) for p in registered}

    status_cache_path = Path("data/product_status_cache.json")
    if status_cache_path.exists():
        status_cache = json.loads(status_cache_path.read_text(encoding="utf-8"))
        suspended = len([s for s in status_cache
                         if s.get("product_id") in registered_ids and s.get("status_type") == "SUSPENSION"])
        if suspended and registered:
            out.append({
                "label": "판매 가능 상품 부족(추정)",
                "detail": f"등록 {len(registered)}개 중 {suspended}개가 판매중지 — 매출 부재의 직접 원인일 수 있음",
                "link": "/products?tab=action",
            })

    sales_records = load_orders()
    t = trend(sales_records, today)
    if t["enough_sample"]:
        contrib = _product_contribution(sales_records, today, t["window_days"])
        movers = [c for c in contrib if c["delta"] != 0]
        if movers:
            name_by_id = {str(p.get("naver_product_id", "")): p.get("name", "") for p in registered}
            top = movers[:3]
            bottom = [c for c in movers[-3:] if c not in top]
            lines = [f"{name_by_id.get(c['naver_product_id'], c['naver_product_id'])[:16]} {c['delta']:+,}원"
                     for c in top + bottom]
            out.append({
                "label": "상품별 매출 증감 기여(추정)",
                "detail": ", ".join(lines),
                "link": "/products",
            })

    r = return_rate(days=30)
    if r["rate"] is not None:
        flag = "기준 초과" if r["rate"] > FAST_SETTLEMENT_MAX_RETURN else "기준 이내"
        out.append({
            "label": "반품률",
            "detail": f"최근 30일 {r['rate']:.0%} ({r['claim_count']}건/{r['order_count']}건) — 빠른정산 기준({FAST_SETTLEMENT_MAX_RETURN:.0%}) {flag}",
            "link": "/cs",
        })
    else:
        out.append({"label": "반품률", "detail": "판단 불가 — 최근 30일 주문 데이터 없음", "link": "/cs"})

    fee = suggest_fee_rate(reconcile())
    if fee:
        out.append({
            "label": "실측 수수료율(추정)",
            "detail": f"실측 {fee['measured_rate']:.1%} vs 가정 {fee['assumed_rate']:.1%} (차이 {fee['diff']:+.1%}p, 표본 {fee['sample_count']}건)",
            "link": "/settlement?tab=reconcile",
        })
    else:
        out.append({"label": "실측 수수료율", "detail": "표본 부족(건별 정산 5건 미만) — 정산 화면에서 동기화 필요", "link": "/settlement?tab=reconcile"})

    sync_cache_path = Path("data/product_sync_cache.json")
    if sync_cache_path.exists():
        sync_cache = json.loads(sync_cache_path.read_text(encoding="utf-8"))
        margin_warn = len([s for s in sync_cache
                           if s.get("naver_product_id") in registered_ids and s.get("action") == ACTION_MARGIN_WARN])
        if margin_warn:
            out.append({
                "label": "도매가 상승에 따른 마진 잠식",
                "detail": f"{margin_warn}건 — 도매가가 올라 최소마진 미달 상태",
                "link": "/products?tab=action",
            })

    return out


def opportunities(candidates: list, sales_records: list) -> dict:
    """놓치고 있는 것 — 미등록 후보는 트랙별로 나눠서 보여준다(6단계 데이터 복구가
    선행돼야 track이 채워짐. 그 전엔 전부 미분류로 정직하게 표시)."""
    niche = [c for c in candidates if c.get("track") == "A"]
    remake = [c for c in candidates if c.get("track") == "B"]
    unclassified = [c for c in candidates if c.get("track") not in ("A", "B")]

    revenue_by_product = {}
    for r in sales_records:
        pid = r.get("naver_product_id") or "미매칭"
        revenue_by_product[pid] = revenue_by_product.get(pid, 0) + r["revenue"]
    total_revenue = sum(revenue_by_product.values())
    concentration = None
    if total_revenue > 0:
        top_pid, top_revenue = max(revenue_by_product.items(), key=lambda kv: kv[1])
        share = top_revenue / total_revenue
        if share >= 0.5:
            concentration = {"naver_product_id": top_pid, "share": share}

    return {
        "niche_count": len(niche), "remake_count": len(remake), "unclassified_count": len(unclassified),
        "concentration": concentration,
    }


def deep_opportunities(candidates: list, limit: int = 3) -> list:
    """(정밀) 진입권장 후보 상위 N개의 검색량 계절성 — 데이터랩 API를 건당 1회
    호출하므로(fetch_trend) 방문마다 자동으로 돌리지 않고 버튼을 눌렀을 때만."""
    from ..sourcing.trend import fetch_trend

    top = sorted((c for c in candidates if c.get("score", 0) >= 55),
                 key=lambda c: c.get("score", 0), reverse=True)[:limit]
    results = []
    for c in top:
        try:
            t = fetch_trend(c["keyword"])
            results.append({"keyword": c["keyword"], "direction": t.direction, "is_seasonal": t.is_seasonal,
                             "variance": t.variance})
        except Exception as e:
            results.append({"keyword": c["keyword"], "error": str(e)})
    return results


def vitals(registered: list) -> dict:
    """스토어 체력 — 기준 대비 판정. 데이터가 없으면 지어내지 않고 None으로 둔다."""
    from ..config import FAST_SETTLEMENT_MAX_RETURN, MIN_MARGIN
    from .claims import return_rate
    from .health import _dispatch_delay_issues
    from .cashflow import cash_events
    from ..margin.calculator import calculate as calc_margin

    r = return_rate(days=30)
    fast_settlement_ok = None if r["rate"] is None else r["rate"] < FAST_SETTLEMENT_MAX_RETURN

    try:
        dispatch_delay_count = len(_dispatch_delay_issues())
    except Exception:
        dispatch_delay_count = None

    margins = [p.get("margin_rate", 0) for p in registered if p.get("margin_rate") is not None]
    avg_margin = sum(margins) / len(margins) if margins else None

    below_min_profit = 0
    for p in registered:
        m = calc_margin(sale_price=p.get("sale_price", 0), cost_price=p.get("supply_price", 0),
                         free_shipping=(p.get("sale_price", 0) >= 30_000))
        if not m.passes_abs_floor:
            below_min_profit += 1

    try:
        events = cash_events()
        cash_balance = events[-1]["balance"] if events else None
    except Exception:
        cash_balance = None

    return {
        "fast_settlement_ok": fast_settlement_ok,
        "dispatch_delay_count": dispatch_delay_count,
        "avg_margin_rate": avg_margin,
        "below_min_profit_count": below_min_profit,
        "cash_balance": cash_balance,
    }


def _verdict(registered: list, t: dict) -> dict:
    import json
    from pathlib import Path

    status_cache_path = Path("data/product_status_cache.json")
    if registered and status_cache_path.exists():
        status_cache = json.loads(status_cache_path.read_text(encoding="utf-8"))
        registered_ids = {str(p.get("naver_product_id", "")) for p in registered}
        suspended = len([s for s in status_cache
                         if s.get("product_id") in registered_ids and s.get("status_type") == "SUSPENSION"])
        if suspended == len(registered):
            return {"level": "주의", "message": f"등록 상품 {len(registered)}개가 모두 판매중지 상태입니다 — 지금 스토어에서 살 수 있는 상품이 없습니다."}
        if suspended:
            return {"level": "주의", "message": f"등록 상품 {len(registered)}개 중 {suspended}개가 판매중지 상태입니다."}

    if not t["enough_sample"]:
        return {"level": "판단 불가", "message": f"최근 데이터가 부족해 종합 판정을 낼 수 없습니다(표본 {t['sample_count']}건)."}
    if t["revenue_delta"] is not None and t["revenue_delta"] < -0.1:
        return {"level": "주의", "message": f"매출이 직전 {t['window_days']}일 대비 {t['revenue_delta']:.0%} 감소했습니다."}
    return {"level": "양호", "message": "특별히 주의할 신호가 없습니다."}


def check_store_health_macro(registered: Optional[list] = None, today: Optional[date] = None,
                              deep: bool = False) -> dict:
    """홈의 health.check_store_health()(개별 문제 나열)와 짝을 이루는 거시 진단.
    deep=False(기본)면 전부 이미 있는 로컬 파일만 읽는다 — 네트워크 호출 없음,
    방문마다 돌려도 가볍다. deep=True면 검색량 계절성까지 추가로 확인한다(API
    호출 있음, 버튼을 눌렀을 때만)."""
    import json
    from pathlib import Path
    from datetime import datetime
    from .sales import load_orders

    registered = registered if registered is not None else (
        json.loads(Path("data/registered_products.json").read_text(encoding="utf-8"))
        if Path("data/registered_products.json").exists() else []
    )
    today = today or date.today()
    sales_records = load_orders()
    candidates = (
        json.loads(Path("data/sourcing_log.json").read_text(encoding="utf-8"))
        if Path("data/sourcing_log.json").exists() else []
    )

    t = trend(sales_records, today)
    result = {
        "checked_at": datetime.now().isoformat(timespec="minutes"),
        "verdict": _verdict(registered, t),
        "trend": t,
        "causes": causes(registered, today),
        "opportunities": opportunities(candidates, sales_records),
        "vitals": vitals(registered),
        "deep": deep,
    }
    if deep:
        try:
            result["deep_trend"] = deep_opportunities(candidates)
        except Exception as e:
            result["deep_trend_error"] = str(e)
    return result


def _demo() -> None:
    """실행 가능한 자체 점검 — 같은 일수 비교와 표본 부족 판정만 검증(파일 IO 없음)."""
    today = date(2026, 8, 30)

    # 표본 부족 — 5건 미만이면 판단 불가
    few = [{"date": "2026-08-29", "revenue": 1000, "profit": 200}]
    t = trend(few, today)
    assert t["enough_sample"] is False and t["sample_count"] == 1, "표본 부족을 못 잡음"

    # 같은 경과일 비교 — 이번 28일 5건(매출 5000) vs 직전 28일 5건(매출 2500) → +100%
    records = []
    for i in range(5):
        records.append({"date": (today - timedelta(days=i)).isoformat(), "revenue": 1000, "profit": 200})
    for i in range(5):
        d = today - timedelta(days=28 + i)
        records.append({"date": d.isoformat(), "revenue": 500, "profit": 100})
    t = trend(records, today, window_days=28)
    assert t["enough_sample"] is True, "표본 10건인데 부족 판정함"
    assert t["this"]["revenue"] == 5000 and t["prev"]["revenue"] == 2500, "기간 분리가 안 맞음(경과일 불공정 비교 의심)"
    assert abs(t["revenue_delta"] - 1.0) < 0.01, "매출 증감률 계산 오류"

    print("store_health self-check OK")


if __name__ == "__main__":
    _demo()
