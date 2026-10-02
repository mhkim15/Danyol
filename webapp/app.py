#!/usr/bin/env python3
"""
Friday — 비브레이브 로컬 운영 대시보드 (데스크탑 브라우저 UI).

Claude 앱 대화 대신 실제 브라우저 화면으로 발굴 후보 확인/등록, 주문 조회/발송처리,
도매매 발주(확인 필수)를 조작한다. 127.0.0.1에만 바인딩되어 이 컴퓨터 밖에서는 접근 불가
(2026-07-13: 우선 로컬 전용으로 구축, 외부 공개는 추후 별도 검토 — 비용·보안 문제로 보류).

실행:
  python3 webapp/app.py
  브라우저에서 http://127.0.0.1:5050 접속
"""
import calendar
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, url_for

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
SOURCING_LOG = DATA_DIR / "sourcing_log.json"
REGISTERED_PRODUCTS = DATA_DIR / "registered_products.json"
TRACKED_PRODUCTS = DATA_DIR / "tracked_products.json"
GENERATED_IMAGES_DIR = DATA_DIR / "generated_images"


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "local-dev-only-not-secret")
# 상세페이지용 사진 업로드 상한 — 없으면 무제한이라 실수로 큰 파일을 올리면 메모리를
# 그대로 먹는다. 폰 사진 10장(장당 5MB급)을 한 번에 올리는 정도는 통과시킨다.
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024


@app.template_global("domemae_url")
def _domemae_url(goods_no):
    """목록·상세의 "도매매 상품 보기" 링크 — 주소 형식은 domemae.py 한 곳에서만 정한다."""
    from bebrave.sourcing.domemae import goods_page_url
    return goods_page_url(goods_no)

FRIDAY_USER = os.environ.get("FRIDAY_USER")
FRIDAY_PASSWORD = os.environ.get("FRIDAY_PASSWORD")


@app.before_request
def _require_login():
    # 외부 배포 시 발주/등록 기능이 인증 없이 노출되지 않도록 강제.
    # FRIDAY_USER/PASSWORD 미설정이면(로컬 전용 실행) 인증 생략.
    if not FRIDAY_USER or not FRIDAY_PASSWORD:
        return
    auth = request.authorization
    ok = (
        auth
        and secrets.compare_digest(auth.username, FRIDAY_USER)
        and secrets.compare_digest(auth.password, FRIDAY_PASSWORD)
    )
    if not ok:
        return Response(
            "로그인이 필요합니다.", 401,
            {"WWW-Authenticate": 'Basic realm="Friday"'},
        )


def _lookup_supply_price(goods_no: str):
    """등록 원장에서 도매매 상품번호로 등록 시점 도매가를 찾는다 — 발주 지출 추정용.
    실제 발주가는 domemae_order.place_order()가 알려주지 않으므로(도매매가 자체 가격으로
    청구), 등록시 기록해둔 supply_price로 근사한다. 못 찾으면 None(미상)."""
    for p in _load_json(REGISTERED_PRODUCTS):
        if p.get("domemae_goods_no") == goods_no:
            return p.get("supply_price")
    return None


def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _extract_pipeline_reasons(buf_text: str, include_info: bool = False) -> list:
    """pipeline.run()/discover()가 stdout에 찍는 중단 사유([건너뜀]/[오류]/[경고], 필요시
    [안내]까지)를 그대로 뽑아온다. register_candidate/discover_scan
    셋이 같은 정규식을 따로 들고 있었다(2026-09) — 한 곳으로 합침."""
    markers = r"건너뜀|오류|경고" + (r"|안내" if include_info else "")
    return [
        m.group(0).strip()
        for line in buf_text.splitlines()
        for m in [re.search(rf"\[(?:{markers})\].*", line)]
        if m
    ]


def _notify(text: str) -> None:
    """카카오 '나에게 보내기'로 알림 발송 — 키 미설정이거나 발송 실패해도 화면 흐름은 절대 안 끊는다.
    (알림은 부가기능이지 핵심 흐름이 아니므로 실패를 조용히 서버 로그로만 남긴다.)"""
    if not (os.environ.get("KAKAO_REST_API_KEY") and os.environ.get("KAKAO_REFRESH_TOKEN")):
        return
    try:
        from bebrave.notify.kakao import send_to_me
        send_to_me(text)
    except Exception as e:
        print(f"[알림 발송 실패] {e}")


def _find_registered_product(order) -> tuple:
    """주문 → (등록상품, 매칭방법). 스마트스토어 상품ID 우선, 실패시 이름 폴백.
    (2026-08 — 이름만 보고 엉뚱한 상품에 발주하는 사고를 막기 위해 ID 매칭을 우선으로 바꿈)"""
    from bebrave.smartstore.purchase_queue import match_order_to_product
    return match_order_to_product(order, _load_json(REGISTERED_PRODUCTS))


# ── 스토어 헬스체크 (거시 진단, 3단계에서 홈과 분리·4단계에서 고도화) ────────────────

def _env_status() -> dict:
    """(설정여부, 필수여부) — 카카오 알림·Claude API는 선택 기능이라 미설정이어도 경고색 안 씀.
    "내 스토어 괜찮나"를 묻는 화면(헬스체크)에 속하는 정보라 홈에서 이관함."""
    return {
        "도매매 (Open API)": (bool(os.environ.get("DOMEMAE_API_KEY")), True),
        "네이버 커머스 API": (bool(os.environ.get("NAVER_COMMERCE_CLIENT_ID")), True),
        "도매매 발주 (Private)": (bool(os.environ.get("DOMEMAE_USER_ID")), True),
        "카카오 알림": (bool(os.environ.get("KAKAO_REST_API_KEY")), False),
        "Claude API": (bool(os.environ.get("ANTHROPIC_API_KEY")), False),
    }


@app.route("/health")
def health_view():
    """스토어 점검 — 성장·운영·돈 세 축을 판정하고 이번 달 전략과 할 일을 낸다(옛 주간리포트 흡수).
    전부 로컬 원장만 읽는다(네트워크 없음)."""
    from bebrave.report.strategy import (collect_metrics, diagnose, load_manual, load_history,
                                         record_prescription, last_result)
    metrics = collect_metrics()
    manual = load_manual()
    result = diagnose(metrics, manual)
    record_prescription(result, metrics)
    return render_template("health.html", r=result, m=metrics, manual=manual,
                           prev=last_result(load_history(), metrics), env_status=_env_status(),
                           checked_at=datetime.now().strftime("%Y-%m-%d %H:%M"))


@app.route("/health/demo")
def health_demo():
    """샘플 — 실데이터와 같은 판정 규칙을 탄다. ?s=시나리오로 전략 유형별 화면을 볼 수 있다."""
    from bebrave.report.strategy import demo_metrics, diagnose, last_result, DEMO_SCENARIOS
    scenario = request.args.get("s", "cleanup")
    if scenario not in DEMO_SCENARIOS:
        scenario = "cleanup"
    metrics, manual, history = demo_metrics(scenario)
    result = diagnose(metrics, manual)
    return render_template("health.html", demo=True, scenario=scenario, scenarios=DEMO_SCENARIOS,
                           r=result, m=metrics, manual=manual, prev=last_result(history, metrics),
                           env_status=_env_status(), checked_at=datetime.now().strftime("%Y-%m-%d %H:%M"))


@app.route("/health/manual", methods=["POST"])
def health_manual():
    """API로 못 받는 값(굿서비스 점수)과 월 순수익 목표를 직접 입력."""
    from bebrave.report.strategy import save_manual

    def num(name, cast):
        v = (request.form.get(name) or "").replace(",", "").strip()
        try:
            return cast(v) if v else None
        except ValueError:
            return None
    gs, goal = num("good_service", float), num("profit_goal", int)
    if gs is not None and not (0 <= gs <= 5):
        flash("굿서비스 점수는 0~5 사이로 입력하세요.", "error")
        return redirect(url_for("health_view"))
    save_manual(good_service=gs, profit_goal=goal)
    flash("저장했습니다 — 판정에 반영됐습니다.", "success")
    return redirect(url_for("health_view"))


@app.route("/health/history/<month>", methods=["POST"])
def health_history_done(month):
    from bebrave.report.strategy import mark_done
    mark_done(month, request.form.get("done") == "1")
    return redirect(url_for("health_view"))


# ── 홈 = 오늘 할 일 (거시 진단은 /health로 분리됨, 4단계) ───────────────────────

def _todo_groups(registered: list, pending_orders, returns_count, inquiry_count) -> list:
    """앉은 자리에서 처리 가능한 단위로 묶은 "오늘 할 일" 목록. 상품 조치의 그룹
    이름은 상품 관리 화면(1단계)과 반드시 일치시킨다 — 다른 이름을 쓰면 같은
    일이 두 개의 다른 일처럼 보인다. 전부 로컬 캐시 기준이라 홈 방문이 느려지지
    않는다(유일한 예외는 이미 다른 이유로 방문마다 돌던 주문 조회 — 아래 참고)."""
    from bebrave.smartstore.purchase_queue import load_queue, STATUS_ORDERED
    from bebrave.report.performance import product_performance
    from bebrave.report.health import check_store_health

    groups = []

    # 품절·재고조정·마진붕괴·발송지연 판정은 health.py(캐시 기반, deep=False)와 공유한다 —
    # 같은 판정을 두 곳에서 따로 하면 두 화면이 다른 답을 낼 수 있다.
    # 네이버 판매중지 자체는 여기서 다루지 않는다 — 사람이 일부러 내렸을 수도 있는
    # 상태라 "할 일"로 단정할 수 없다(상품 관리 화면에서 직접 판단할 문제).
    issues_by_category = {}
    for issue in check_store_health(deep=False):
        issues_by_category.setdefault(issue.category, []).append(issue)

    order_items = []
    # None은 "0건"이 아니라 "조회 실패"다 — 둘을 같이 취급하면 발주할 주문이 쌓여 있는데도
    # 홈이 "지금 할 일 없음"을 띄운다(네트워크가 끊긴 화면과 깨끗한 화면이 구별 안 됨).
    if pending_orders is None:
        order_items.append({"label": "발주", "unknown": True})
    elif pending_orders:
        order_items.append({"label": "발주", "link": url_for("orders", tab="ready"), "n": pending_orders})
        # 발주할 주문이 있어도 이머니가 모자라면 아무것도 못 한다 — 들어가서 알기 전에
        # 여기서 알려준다. 잔액 조회가 실패해도 발주 할 일 자체는 그대로 보여야 하므로 조용히 넘어간다.
        try:
            from bebrave.sourcing.domemae_order import login, fetch_emoney_balance
            from bebrave.smartstore.purchase_queue import STATUS_READY
            needed = sum((_lookup_supply_price(i.get("matched_goods_no", "")) or 0) * i.get("quantity", 1)
                          for i in load_queue() if i["status"] == STATUS_READY)
            cash = fetch_emoney_balance(login()["sId"])["cash"]
            if needed and cash < needed:
                order_items.append({"label": f"이머니 충전 ({needed - cash:,}원 부족)",
                                    "link": url_for("orders", tab="ready"), "n": 1})
        except Exception:
            pass
    # 발송을 한 덩어리로 세면 페널티가 걸린 지연 건이 평범한 대기 건에 묻힌다 —
    # 결제 후 24시간 넘은 건을 따로 뽑는다(합은 전체 발송 대기와 같다).
    dispatch_wait = len([i for i in load_queue() if i["status"] == STATUS_ORDERED])
    delay_n = len(issues_by_category.get("발송지연", []))
    if delay_n:
        order_items.append({"label": "발송 지연", "link": url_for("orders", tab="dispatch"), "n": delay_n})
    if dispatch_wait - delay_n > 0:
        order_items.append({"label": "발송", "link": url_for("orders", tab="dispatch"),
                            "n": dispatch_wait - delay_n})
    groups.append({"name": "주문", "rows": order_items})

    supply_n = len(issues_by_category.get("품절", [])) + len(issues_by_category.get("재고조정", []))
    margin_n = len(issues_by_category.get("마진붕괴", []))
    no_sale_n = len([p for p in product_performance(registered=registered) if p["status"].startswith("무판매")])

    # 셋이 서로 다른 일인데 링크가 전부 같은 "조치 필요" 필터로 가고 있었다 —
    # 상품 관리에 성격별 필터가 이미 있으므로 각각 그리로 보낸다.
    product_items = []
    if supply_n:
        product_items.append({"label": "재고 확인", "link": url_for("products_view", tab="stock"), "n": supply_n})
    if margin_n:
        product_items.append({"label": "마진 확인", "link": url_for("products_view", tab="margin"), "n": margin_n})
    if no_sale_n:
        product_items.append({"label": "판매 점검", "link": url_for("products_view", tab="nosale"), "n": no_sale_n})
    groups.append({"name": "상품", "rows": product_items})

    cs_items = []
    if returns_count is None:
        cs_items.append({"label": "반품·취소", "unknown": True})
    elif returns_count:
        cs_items.append({"label": "반품·취소", "link": url_for("cs"), "n": returns_count})
    if inquiry_count is None:
        cs_items.append({"label": "답변", "unknown": True})
    elif inquiry_count:
        cs_items.append({"label": "답변", "link": url_for("cs"), "n": inquiry_count})
    groups.append({"name": "고객응대", "rows": cs_items})

    settle_items = []
    try:
        from bebrave.report.reconcile import reconcile, suggest_fee_rate
        s = suggest_fee_rate(reconcile())
        if s and abs(s["diff"]) > 0.01:
            settle_items.append({"label": f"수수료율 확인 ({s['diff']:+.1%}p)",
                                  "link": url_for("settlement_view", tab="reconcile"), "n": 1})
    except Exception:
        pass  # 표본 부족(5건 미만)이면 suggest_fee_rate가 None — 지어내지 않고 그냥 0건으로 둔다
    groups.append({"name": "정산", "rows": settle_items})

    for g in groups:
        # 조회 실패 행은 건수를 모르므로 합계에 넣지 않는다 — 모르는 걸 0으로도 1로도 세지 않는다.
        g["count"] = sum(i["n"] for i in g["rows"] if not i.get("unknown"))
    return groups


def _prev_month_compare(month_series_fn, sales_records, claims, today: date) -> dict:
    """이번 달 1일~오늘 vs 지난달 1일~같은 날짜. 지난달 전체와 비교하면 월초엔 늘 '급감'으로
    보여서 쓸모가 없다 — 같은 경과일수끼리 비교한다. 지난달이 더 짧으면 말일까지만."""
    py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    days = min(today.day, calendar.monthrange(py, pm)[1])
    cur = month_series_fn(sales_records, today.year, today.month)[:today.day]
    prev = month_series_fn(sales_records, py, pm)[:days]

    def totals(series, ym, last_day):
        return {
            "revenue": sum(p["revenue"] for p in series),
            "profit": sum(p["profit"] for p in series),
            "order_count": sum(p["order_count"] for p in series),
            "returns": len([c for c in claims
                            if c.get("claimed_at", "")[:7] == ym and int(c["claimed_at"][8:10] or 0) <= last_day]),
        }

    this = totals(cur, f"{today.year:04d}-{today.month:02d}", today.day)
    last = totals(prev, f"{py:04d}-{pm:02d}", days)
    # 지난달이 0이면 %가 정의되지 않는다 — 0을 넣으면 "변화 없음"으로 읽히므로 None으로 둔다.
    delta = {k: (None if not last[k] else (this[k] - last[k]) / abs(last[k])) for k in this}
    return {"days": days, "prev_month": pm, "last": last, "delta": delta}


@app.route("/")
def index():
    from bebrave.report import load_sales_orders, sales_month_series
    from bebrave.report.claims import load_claims

    candidates = _load_json(SOURCING_LOG)
    registered = _load_json(REGISTERED_PRODUCTS)
    checked_at = datetime.now().strftime("%H:%M")

    # 처리 대기 주문 — 최근 24시간 내 결제완료(PAYED)로 바뀐 뒤 아직 발송처리 안 된 건수.
    # 조회한 김에 매출 원장에도 바로 반영해서(record_sales_orders) 방문할 때마다
    # 자동으로 최신화되게 함 — 별도 "새로고침" 버튼/API 호출 불필요. (홈의 유일한 실시간 조회)
    pending_orders = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        from bebrave.report import record_sales_orders
        token = get_access_token()
        recent_orders = fetch_new_orders(token, hours=24)
        pending_orders = len([o for o in recent_orders if o.status == "PAYED"])
        record_sales_orders(recent_orders)
    except Exception:
        pending_orders = None  # API 미연동/실패 시 화면에서 "확인 필요"로 표시

    # 반품·취소 — 최근 24시간 내 클레임 접수 건수 (별도 lastChangedType 조회라 실패해도 위 주문 조회엔 영향 없음)
    returns_count = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        token = get_access_token()
        returns_count = len(fetch_new_orders(token, hours=24, status_type="CLAIM_REQUESTED"))
    except Exception:
        returns_count = None

    inquiry_count = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.inquiries import fetch_inquiries
        token = get_access_token()
        inquiry_count = len(fetch_inquiries(token, days=7, answered=False))
    except Exception:
        inquiry_count = None

    todo_groups = _todo_groups(registered, pending_orders, returns_count, inquiry_count)
    todo_total = sum(g["count"] for g in todo_groups)

    sales_records = load_sales_orders()
    today = date.today()
    selected_year = request.args.get("year", type=int) or today.year
    selected_month = request.args.get("month", type=int) or today.month
    if (selected_year, selected_month) > (today.year, today.month):
        selected_year, selected_month = today.year, today.month

    # 차트는 이동 가능하지만, "이번 달" 숫자카드 4개는 항상 실제 이번 달 기준이다 —
    # 지난달을 보고 있다고 이번달 매출 카드까지 지난달 값으로 바뀌면 헷갈린다.
    chart_series = sales_month_series(sales_records, selected_year, selected_month)
    is_current_month = (selected_year, selected_month) == (today.year, today.month)
    current_series = chart_series if is_current_month else sales_month_series(sales_records, today.year, today.month)
    this_month = {
        "revenue": sum(p["revenue"] for p in current_series),
        "profit": sum(p["profit"] for p in current_series),
        "order_count": sum(p["order_count"] for p in current_series),
        "uncertain_count": sum(p.get("uncertain_count", 0) for p in current_series),
    }
    month_prefix = today.strftime("%Y-%m")
    claims = load_claims()
    this_month_returns = len([c for c in claims if c.get("claimed_at", "").startswith(month_prefix)])
    compare = _prev_month_compare(sales_month_series, sales_records, claims, today)

    prev_month, prev_year = (12, selected_year - 1) if selected_month == 1 else (selected_month - 1, selected_year)
    next_month, next_year = (1, selected_year + 1) if selected_month == 12 else (selected_month + 1, selected_year)
    next_disabled = (next_year, next_month) > (today.year, today.month)
    # 차트의 회색 점선 — 보고 있는 달의 직전 달
    chart_prev_series = sales_month_series(sales_records, prev_year, prev_month)

    return render_template(
        "index.html",
        todo_groups=todo_groups, todo_total=todo_total, checked_at=checked_at,
        this_month=this_month, this_month_returns=this_month_returns, chart_series=chart_series,
        compare=compare, chart_prev_series=chart_prev_series,
        selected_year=selected_year, selected_month=selected_month,
        prev_year=prev_year, prev_month=prev_month, next_year=next_year, next_month=next_month,
        next_disabled=next_disabled,
        env_status=_env_status(),
    )


@app.route("/demo")
def index_demo():
    """홈 화면 전체 구조를 실제 API/데이터 없이 확인하는 샘플 뷰. 지금 비어 있거나
    IP 차단으로 막힌 주문·매출·반품만 가짜 값으로 채운다 — 전부 새로 지어내면
    오히려 실제 화면과 감이 달라진다."""
    from bebrave.report.sales import month_series

    today = date.today()
    demo_sales_records = [
        {"date": (today.replace(day=1)).isoformat(), "revenue": 6600, "profit": 1332},
        {"date": (today.replace(day=min(today.day, 5))).isoformat(), "revenue": 4600, "profit": 944},
        {"date": (today.replace(day=min(today.day, 10))).isoformat(), "revenue": 13000, "profit": None},
    ]
    pm_first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    demo_sales_records += [
        {"date": pm_first.replace(day=d).isoformat(), "revenue": r, "profit": pr}
        for d, r, pr in [(2, 5200, 1100), (8, 9800, 2000), (15, 4600, 900), (21, 7400, 1500)]
    ]
    demo_claims = [{"claimed_at": today.replace(day=1).isoformat()}, {"claimed_at": pm_first.replace(day=3).isoformat()},
                   {"claimed_at": pm_first.replace(day=9).isoformat()}]
    chart_series = month_series(demo_sales_records, today.year, today.month)
    chart_prev_series = month_series(demo_sales_records, pm_first.year, pm_first.month)
    compare = _prev_month_compare(month_series, demo_sales_records, demo_claims, today)
    this_month = {
        "revenue": sum(p["revenue"] for p in chart_series),
        "profit": sum(p["profit"] for p in chart_series),
        "order_count": sum(p["order_count"] for p in chart_series),
        "uncertain_count": sum(p.get("uncertain_count", 0) for p in chart_series),
    }

    demo_groups = [
        {"name": "주문", "count": 3, "rows": [
            {"label": "발주", "link": url_for("orders_demo", tab="ready"), "n": 2},
            {"label": "발송 지연", "link": url_for("orders_demo", tab="dispatch"), "n": 1},
        ]},
        {"name": "상품", "count": 2, "rows": [
            {"label": "판매 점검", "link": url_for("products_view", tab="nosale"), "n": 2},
        ]},
        {"name": "고객응대", "count": 1, "rows": [
            {"label": "답변", "link": url_for("cs"), "n": 1},
            # 조회 실패 상태도 샘플에 넣는다 — 실제 화면에서 이 줄이 어떻게 보이는지
            # 확인할 방법이 달리 없다(API가 정상일 땐 재현이 안 됨).
            {"label": "반품·취소", "unknown": True},
        ]},
        {"name": "정산", "count": 0, "rows": []},
    ]

    # 상단 초록 안내 배너는 뺐다(2026-09) — 샘플 여부는 제목 옆 "실데이터 보기" 버튼으로 드러난다.
    return render_template(
        "index.html",
        todo_groups=demo_groups, todo_total=6, checked_at=datetime.now().strftime("%H:%M"),
        this_month=this_month, this_month_returns=1, chart_series=chart_series,
        compare=compare, chart_prev_series=chart_prev_series,
        selected_year=today.year, selected_month=today.month,
        prev_year=today.year, prev_month=today.month, next_year=today.year, next_month=today.month,
        next_disabled=True,
        env_status=_env_status(), demo=True,
    )


# ── 발굴 후보 ─────────────────────────────────────────────────────────────

def _category_groups(targets) -> list:
    """발굴 화면에서 스캔 카테고리를 네이버 1단계 카테고리별로 묶는다 — [(묶음, [카테고리...])].
    묶음 정의(config.TARGET_CATEGORY_GROUPS)에 빠진 대상은 "기타"로 모아 화면에서 사라지지 않게 한다."""
    from bebrave.config import TARGET_CATEGORY_GROUPS
    grouped = [(g, [c for c in cats if c in targets]) for g, cats in TARGET_CATEGORY_GROUPS.items()]
    placed = {c for _, cats in grouped for c in cats}
    rest = [c for c in targets if c not in placed]
    return [(g, cats) for g, cats in grouped if cats] + ([("기타", rest)] if rest else [])


def _candidate_bucket(c: dict) -> str:
    """트랙(할 일의 종류)으로 후보를 나눈다 — 데이터 종류가 아니라 해야 하는 일의
    종류로 나누라는 설계 원칙. 6단계 데이터 복구 전 저장분은 track이 없어
    자동으로 미분류에 남는다(억지로 추측해서 분류하지 않는다)."""
    track = c.get("track", "")
    if track not in ("A", "B"):
        return "unclassified"
    if c.get("recommendation", "") in ("보류", "제외"):
        return "hold"
    return "niche" if track == "A" else "remake"


@app.route("/candidates")
def candidates():
    return _candidates_page(_load_json(SOURCING_LOG))


# 발굴 후보 샘플 — 스캔 결과가 비어 있어도 화면 구조를 볼 수 있게. 점수·월검색수는 지어낸 값이지만
# 도매매 상품은 실제 상품번호다(2026-09-30 조회: 상세 이미지 사용 허용 + 재고 있음) — 그래야
# 상품명을 눌러 상세 미리보기(상세페이지·이미지 최적화 등)까지 실제로 열어볼 수 있다.
# 도매매에서 내려가면 그 행의 미리보기만 "조회 실패"로 뜬다 — 그때 번호만 바꿔 넣으면 된다.
_DEMO_CANDIDATES = [
    # (카테고리, 키워드, 트랙, 점수, 월검색수, 도매매 상품번호, 상품명, 판매가, 도매가, 계절성)
    ("수납/정리용품", "냉장고 정리 트레이", "A", 68, 8200, "44286595", "칸막이 정리함 다용도 수납 냉장고정리 트레이", 11100, 4900, False),
    ("욕실용품", "규조토 발매트", "A", 61, 12400, "49637264", "발매트 빨아쓰는 프리미엄 규조토발매트 욕실 주방 현관 논슬립", 10300, 4200, False),
    ("네일케어", "큐티클 니퍼", "A", 57, 5300, "68084192", "큐티클 니퍼 큐티클관리니퍼 풋케어 네일", 13600, 7200, False),
    ("원예/식물", "행잉 플랜터", "A", 49, 3900, "64725330", "걸이용 미니 화분 행잉플랜터 다육이화분 식물걸이화분", 8700, 2800, True),
    ("청소용품", "틈새 청소 브러시", "A", 44, 6100, "68127749", "자동차 송풍구 틈새 청소 브러시 2in1 2개입", 13100, 6700, False),
    # 개당 이익이 작은 예 — 판매가가 낮으면 수수료·배송비를 빼고 남는 게 적다는 걸 보이게
    ("세탁용품", "세탁망", "A", 42, 4800, "60066783", "셀링온 건조기세탁망", 6700, 1000, False),
    ("침구단품", "메모리폼 베개커버", "B", 52, 21000, "34722296", "피그먼트 메모리폼베개 경추굴곡형 목베개 순면 커버", 15100, 8500, False),
    ("요가/필라테스", "필라테스 링", "B", 47, 16500, "32111778", "종아리 요가링 마사지링 필라테스 스트레칭 하드타입 2P", 8200, 2300, False),
    ("헤어케어", "두피 마사지 브러시", "B", 45, 27800, "51115298", "샴푸 브러쉬 헤어 두피 마사지 브러시", 6300, 650, False),
    ("욕실용품", "욕실 선반", "B", 41, 45000, "62879746", "360도 회전 욕실선반 2개세트", 19700, 12700, False),
]


@app.route("/candidates/demo")
def candidates_demo():
    items = [{"category": cat, "keyword": kw, "track": tr, "score": sc, "monthly_search": ms,
              "supply_name": name, "supply_goods_no": goods_no, "est_sale_price": sale,
              "est_cost_price": cost, "margin_rate": round((sale - cost) / sale, 2) if sale else None,
              "is_seasonal": seas, "image_usable": True, "recommendation": ""}
             for cat, kw, tr, sc, ms, goods_no, name, sale, cost, seas in _DEMO_CANDIDATES]
    return _candidates_page(items, demo=True)


def _candidates_page(items: list, demo: bool = False):
    from collections import Counter
    from bebrave.config import TARGET_CATEGORIES
    from bebrave.margin.calculator import calculate as calc_margin
    from bebrave.sourcing.discover import _recommendation

    items.sort(key=lambda c: c.get("score", 0), reverse=True)

    # 이미 등록한 도매매 상품은 목록에서 뺀다 — 예전엔 "✓ 등록됨" 배지만 달고 그대로
    # 남겨뒀는데, 다시 눌러 등록하면 중복 가드에 막혀 이유 없이 실패했다(2026-09).
    # 할 일이 남은 것만 보이는 게 목록의 역할이다.
    registered_goods_nos = {
        p.get("domemae_goods_no") for p in _load_json(REGISTERED_PRODUCTS) if p.get("domemae_goods_no")
    }
    items = [c for c in items
             if not (c.get("supply_goods_no") and c["supply_goods_no"] in registered_goods_nos)]
    # 도매매가 상세설명 이미지 사용을 허용한 상품만 보인다(2026-09) — 허용 안 된 이미지로
    # 상세페이지를 만들면 저작권 문제. 확인 안 된 후보도 숨긴다("허용된 것만"이 기준).
    items = [c for c in items if c.get("image_usable") is True]

    # 보류·제외는 보여주지 않는다(2026-09) — 점수 미달이라 팔아도 남기기 어렵다. 스캔은 아예
    # 저장하지 않지만 CLI 등 다른 경로로 들어온 기록이 있을 수 있어 여기서도 거른다.
    items = [c for c in items if _candidate_bucket(c) != "hold"]
    counts = {"niche": 0, "remake": 0, "unclassified": 0}
    for c in items:
        counts[_candidate_bucket(c)] += 1

    # 기본 탭이 틈새 고정이라, 옛 스캔 데이터처럼 전부 미분류면 첫 화면이 빈 표였다.
    default_tab = next((t for t in ("niche", "remake", "unclassified") if counts[t]), "niche")
    tab = request.args.get("tab", default_tab)
    if tab not in counts:
        tab = default_tab
    unconfirmed_only = request.args.get("unconfirmed") == "1"

    filtered = [c for c in items if _candidate_bucket(c) == tab]

    for c in filtered:
        # 점수 숫자만으로는 진입해도 되는지 판단이 안 된다 — 합격선 판정을 화면에도 쓴다.
        # 판정 기준은 소싱 로직과 같은 함수를 그대로 재사용(두 곳에서 따로 정하지 않는다).
        c["verdict"] = _recommendation(c.get("score", 0), "", track=c.get("track", "A"))
        # 마진율만 보면 "50%인데 개당 900원"을 못 거른다 — 등록 단계로 넘기기 전에 절대금액.
        sale = c.get("est_sale_price") or 0
        cost = c.get("est_cost_price") or 0
        c["margin_amount"] = calc_margin(sale_price=sale, cost_price=cost).net_profit if sale and cost else None

    return render_template("candidates.html", candidates=filtered, target_categories=TARGET_CATEGORIES,
                            category_groups=_category_groups(TARGET_CATEGORIES),
                            tab=tab, counts=counts, unconfirmed_only=unconfirmed_only,
                            # 결과 목록의 카테고리 필터 — 스캔 필터와 같은 1단계 묶음으로, 지금 탭에 있는 것만
                            list_groups=_category_groups(sorted({c.get("category", "") for c in filtered if c.get("category")})),
                            cat_counts=Counter(c.get("category", "") for c in filtered),
                            cat_group={cat: g for g, cats in _category_groups(sorted({c.get("category", "") for c in filtered if c.get("category")})) for cat in cats},
                            verdicts=[v for v in ("진입 권장", "진입 가능") if any(c["verdict"] == v for c in filtered)],
                            scan_last=None if demo else _SCAN["last"], demo=demo,
                            page_ep="candidates_demo" if demo else "candidates")


@app.route("/candidates/confirm_match_bulk", methods=["POST"])
def confirm_match_bulk():
    """도매매 매칭이 '불확실'로 뜬 후보를 사람이 실물/상세페이지 보고 승인 처리 —
    체크한 여러 건을 한 번에. 로컬 JSON만 바꾸고 되돌리기 쉬우므로 확인 게이트를
    두지 않는다(단건 confirm_match는 이 라우트로 통합돼 삭제됨)."""
    pairs = set()
    for raw in request.form.getlist("ids"):
        if "||" in raw:
            kw, tr = raw.split("||", 1)
            pairs.add((kw, tr))

    items = _load_json(SOURCING_LOG)
    n = 0
    for c in items:
        if (c.get("keyword", ""), c.get("track", "")) in pairs:
            c["human_confirmed"] = True
            n += 1
    if n:
        with open(SOURCING_LOG, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        flash(f"실물확인 완료 처리 — {n}건", "success")
    else:
        flash("선택된 후보가 없습니다.", "error")
    return redirect(url_for("candidates", tab=request.form.get("tab", "niche")))


@app.route("/candidates/clear_stale", methods=["POST"])
def clear_stale_candidates():
    """월검색수·트랙 정보가 전혀 없는 옛 스캔분을 정리 — 재계산할 근거가 없어
    복구가 불가능하므로 삭제하고 재스캔을 유도한다. 지금까지 후보를 지울 방법이
    아예 없어서, 로직이 두 번 바뀌는 동안 근거 없는 행이 계속 쌓여 있었다(2026-09).
    실수로 지운 경우를 대비해 삭제 전 스냅샷을 남긴다."""
    from bebrave.sourcing.analyzer import load_from_json, save_to_json
    items = load_from_json(SOURCING_LOG)
    stale = [c for c in items if not c.track and not c.monthly_search]
    keep = [c for c in items if not (not c.track and not c.monthly_search)]

    if not stale:
        flash("정리할 옛 스캔분이 없습니다.", "success")
        return redirect(url_for("candidates"))

    backup_path = DATA_DIR / f"sourcing_log_stale_backup_{date.today().isoformat()}.json"
    save_to_json(stale, backup_path)
    save_to_json(keep, SOURCING_LOG)
    flash(f"옛 스캔분 {len(stale)}건 정리 — 백업: {backup_path.name}. 카테고리를 골라 재스캔하세요.", "success")
    return redirect(url_for("candidates"))


# ── 스캔 — 서버 뒤에서 돈다 ─────────────────────────────────────────────
# 전체 18개 카테고리면 30분 가까이 걸려, 요청 하나로 기다리면 브라우저·연결이 끊긴다(2026-09).
_SCAN = {"running": False, "categories": [], "total": 0, "done": 0, "current": "", "added": 0,
         "started": 0.0, "last": None}
_SCAN_LOCK = threading.Lock()


class _ThreadStdout:
    """스캔 스레드가 찍는 줄만 따로 모은다 — contextlib.redirect_stdout은 프로세스 전체의
    출력을 바꿔서, 30분 스캔 동안 다른 요청의 출력까지 섞인다."""
    # ponytail: 그 사이 다른 요청이 redirect_stdout을 쓰면 몇 줄이 그쪽으로 샐 수 있다 — 스캔을 별도 프로세스로 돌리면 해결

    def __init__(self, target, thread_id):
        self.target, self.thread_id, self.buf = target, thread_id, io.StringIO()

    def write(self, text):
        return (self.buf if threading.get_ident() == self.thread_id else self.target).write(text)

    def flush(self):
        self.target.flush()


def _run_scan(categories: list) -> None:
    from bebrave.sourcing.analyzer import load_from_json, save_to_json, dedupe_by_supply
    from bebrave.sourcing.discover import discover, to_product_candidates

    router = _ThreadStdout(sys.stdout, threading.get_ident())
    sys.stdout = router
    added, dupes_removed, stale_cleared, error = {}, 0, 0, ""
    try:
        for i, category in enumerate(categories):
            _SCAN.update(done=i, current=category)
            result = discover(category=category, limit=15)
            # 목록에 나올 후보만 저장한다 — 보류·제외는 점수 미달이라 팔아도 남기기 어렵고, 도매매
            # 이미지 사용 허용이 확인 안 된 후보(도매매 조회 대상 밖 포함)는 목록에서 숨겨진다.
            # 안 거르면 "신규 후보 21개"라고 해놓고 목록엔 안 보이는 후보만 파일에 쌓였다(2026-09).
            fresh = [c for c in to_product_candidates(result)
                     if c.recommendation not in ("보류", "제외") and c.image_usable is True]
            # 카테고리마다 파일을 새로 읽어 합친다 — 스캔 도중 사람이 실물확인 등으로 바꾼 걸 덮지 않게
            existing = load_from_json(SOURCING_LOG)
            # 트랙·검색량이 없는 옛 스캔분은 같은 카테고리를 다시 스캔하는 이 시점에 치운다
            stale = [c for c in existing if c.category == category and not c.track and not c.monthly_search]
            existing = [c for c in existing if c not in stale]
            stale_cleared += len(stale)
            # (키워드, 트랙)으로 중복을 거른다 — 키워드만 보면 두 트랙 중 한쪽이 조용히 사라진다
            have = {(c.keyword, c.track) for c in existing}
            n = 0
            for c in fresh:
                if (c.keyword, c.track) not in have:
                    existing.append(c)
                    have.add((c.keyword, c.track))
                    n += 1
            existing, dupes = dedupe_by_supply(existing)
            dupes_removed += len(dupes)
            save_to_json(existing, SOURCING_LOG)
            added[category] = n
            _SCAN["added"] += n
    except Exception as e:
        error = f"스캔 실패({_SCAN['current']}): {e}"
    finally:
        if sys.stdout is router:
            sys.stdout = router.target
        # discover()는 실패해도 예외 없이 [경고]만 찍고 빈 결과를 낸다 — 그 줄을 화면으로 올린다
        problems = _extract_pipeline_reasons(router.buf.getvalue(), include_info=True)
        problems = list(dict.fromkeys(([error] if error else []) + problems))[:8]
        detail = ", ".join(f"{k} {v}개" for k, v in added.items() if v)
        message = (f"{len(added)}/{len(categories)}개 카테고리 · 신규 후보 {sum(added.values())}개"
                   + (f"({detail})" if detail else "")
                   + (f" · 동일상품 중복 {dupes_removed}개 제거" if dupes_removed else "")
                   + (f" · 옛 미분류 {stale_cleared}건 정리" if stale_cleared else ""))
        _SCAN.update(running=False, done=len(added),
                     last={"finished": datetime.now().strftime("%m-%d %H:%M"),
                           "message": message, "problems": problems})


@app.route("/candidates/discover", methods=["POST"])
def discover_scan():
    """스캔을 뒤에서 시작하고 바로 목록으로 돌아간다 — 진행은 화면이 candidates_scan_status로
    3초마다 가져간다. 한 번 스캔하면 틈새·리메이크 후보가 함께 나온다."""
    from bebrave.config import TARGET_CATEGORIES
    tab = request.form.get("tab") or None
    categories = [c for c in request.form.getlist("categories") if c in TARGET_CATEGORIES]
    if not categories:
        flash("스캔할 카테고리를 하나 이상 선택하세요.", "error")
        return redirect(url_for("candidates", tab=tab))
    with _SCAN_LOCK:
        if _SCAN["running"]:
            flash("이미 스캔이 진행 중입니다 — 끝난 뒤 다시 누르세요.", "error")
            return redirect(url_for("candidates", tab=tab))
        _SCAN.update(running=True, categories=categories, total=len(categories), done=0,
                     current=categories[0], added=0, started=time.time())
    threading.Thread(target=_run_scan, args=(categories,), daemon=True).start()
    return redirect(url_for("candidates", tab=tab))


@app.route("/candidates/scan_status")
def candidates_scan_status():
    return {"running": _SCAN["running"], "total": _SCAN["total"], "done": _SCAN["done"],
            # 스캔 중에 화면을 다시 열면 태그가 기본값(전체)으로 보여 뭘 스캔하는지 헷갈렸다
            "categories": _SCAN["categories"] if _SCAN["running"] else [],
            "current": _SCAN["current"], "added": _SCAN["added"],
            "elapsed": int(time.time() - _SCAN["started"]) if _SCAN["running"] else 0}


@app.route("/candidates/preview")
def candidates_preview():
    """상품명 최적화 · 태그 · 카테고리 · 마진을 실제 등록 전에 확인하는 미리보기.

    ?modal=1로 호출하면 발굴후보 목록에서 모달로 띄우기 위해 레이아웃 없이
    본문(preview_content.html)만 반환한다.
    """
    keyword = request.args.get("keyword", "")
    is_modal = request.args.get("modal") == "1"
    track = request.args.get("track", "")
    goods_no_hint = request.args.get("goods_no", "")
    # 샘플 목록에서 연 미리보기 — 상품은 실제 도매매 상품이라 나머지는 그대로 보여주고 등록만 막는다
    demo = request.args.get("demo") == "1"
    ctx = {"keyword": keyword, "modal": is_modal, "track": track, "demo": demo}

    def _fail(message):
        if is_modal:
            return f'<div class="flash flash-error">{message}</div>', 200
        flash(message, "error")
        return redirect(url_for("candidates_demo" if demo else "candidates"))

    try:
        from bebrave.sourcing.domemae import search_products, fetch_product_detail, find_matching_product
        from bebrave.margin.calculator import calculate as calc_margin
        from bebrave.smartstore.content import generate_product_content
        from bebrave.smartstore.category import get_category_id, describe_category
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.pipeline import _decide_sale_price

        detail_fetch_error = ""
        matched = True
        if goods_no_hint:
            # 후보 목록에 저장된 도매매 상품번호가 있으면 그걸 바로 조회한다 — 키워드로
            # 재검색하면 그 사이 도매매 재고/가격이 바뀌어 목록에 보이던 상품과 다른
            # 상품이 뜰 수 있었다(2026-09 발견, B-5). goods_no가 없는 옛 후보만 예전처럼
            # 키워드 검색으로 대체한다.
            try:
                p = fetch_product_detail(goods_no_hint)
            except Exception as e:
                return _fail(f"저장된 도매매 상품({goods_no_hint}) 조회 실패: {e}")
        else:
            result = search_products(keyword, limit=10)
            if not result.products:
                return _fail(f"'{keyword}' 도매매 검색 결과 없음")

            # 최저가를 무조건 고르지 않고, discover()와 동일한 형태일치 검증을 거친다 —
            # 그냥 최저가를 집으면 재료/부자재가 완제품으로 둔갑하는 문제가 있었다(2026-08).
            p, matched = find_matching_product([keyword], result.products)
            if p is None:
                return _fail(f"'{keyword}' 도매매 매칭 후보 없음")
            # 상세조회 실패를 조용히 넘기면 getItemList 결과(이미지 1장·설명 0자·재고 0)가
            # 그대로 남아 "부실 리스팅" 경고가 허위로 뜨고, 등록 시엔 이유 설명 없이 스킵된다
            # (2026-09 발견) — 실패 사실을 화면에 남긴다.
            if p.goods_no:
                try:
                    p = fetch_product_detail(p.goods_no)
                except Exception as e:
                    detail_fetch_error = str(e)

        sale_price = _decide_sale_price(p.supply_price, p.retail_price)
        margin = calc_margin(sale_price=sale_price, cost_price=p.supply_price, free_shipping=(sale_price >= 30_000))

        # 카테고리를 먼저 확정해야 태그 생성이 리프 카테고리명을 걸러낼 수 있다(가이드
        # 11쪽 "카테고리명은 태그로 사용 불가") — 실제 등록 파이프라인(pipeline.py)과
        # 같은 순서로 옮겼다(2026-09).
        cat_id, cat_name, cat_error = "", "", ""
        try:
            token = get_access_token()
            cat_id = get_category_id(keyword, p.category, token)
            if not cat_id:
                cat_name = "매칭 실패 — 수동 확인 필요"
                cat_error = "카테고리 자동 매칭 실패 — 이 상태로 등록하면 파이프라인이 건너뜁니다"
            else:
                cat_name = describe_category(cat_id, token)
        except Exception as e:
            cat_name = f"조회 실패: {e}"
            cat_error = f"카테고리 조회 실패 — 등록이 실패합니다: {e}"

        content = generate_product_content(keyword, p, sale_price, category_name=cat_name if cat_id else "")

        # 상세페이지 AI 버전 — 저장된 게 있을 때만 보여준다(2026-09). 만드는 일은 화면의
        # "AI로 만들기" 버튼이 단계별로 한다(ai_build_candidate). 예전엔 이 화면을 열기만
        # 해도 키 없이 규칙으로 초안을 만들어 저장했고, 그게 기본 등록본이 됐다.
        # 여기서는 로컬 컷 주소로 보여주고, 등록할 때 pipeline이 같은 컷을 네이버로 올려
        # 주소를 바꿔 끼운다.
        from bebrave.smartstore.cuts import load_seller_note, seller_cuts
        from bebrave.smartstore.pipeline import _build_cut_detail
        seller_note = load_seller_note(goods_no_hint) if goods_no_hint else ""
        built = None
        if p.goods_no:
            built = _build_cut_detail(
                p, "", dry_run=True,
                cut_url=lambda g, f: url_for("cut_image", goods_no=g, filename=f))
        detail_basic = content["detail_content"]     # 도매 원본
        detail_ai = built[0] if built else ""        # 저장된 AI 버전
        if built:
            content["detail_content"] = built[0]
        seller_photos = seller_cuts(p.goods_no) if p.goods_no else []

        # 구성 손보기 화면용 — 컷 목록과 현재 판독·문구. 사람이 여기서 컷을 빼거나
        # 칸별 글을 고치면 판독·문구 파일을 덮어쓰고 페이지를 다시 짠다.
        from bebrave.smartstore.cuts import load_cuts
        from bebrave.smartstore.cut_reader import load_reading
        from bebrave.smartstore.layout import Copy, load_copy
        _cs = load_cuts(p.goods_no) if p.goods_no else None
        _rd = load_reading(p.goods_no) if p.goods_no else None
        _cp = (load_copy(p.goods_no) if p.goods_no else None) or Copy()
        edit_cuts = []
        if _cs and _rd:
            for c in _cs.cuts:
                r = _rd.of(c.index)
                edit_cuts.append({
                    "index": c.index, "source": c.source, "kind": r.kind, "use": r.use,
                    "text": r.text, "label": r.label, "reason": r.reason, "recut": r.recut,
                    # 초안에서 뺀 컷(겹침·정보 적음) — 사진 고르기에 "추천 낮음"과 이유를 보여준다
                    "draft": _rd.in_draft(c.index), "why": r.why,
                    "url": url_for("cut_image", goods_no=p.goods_no, filename=c.filename),
                    # 원본에서의 위치 — 잘못 잘린 컷의 범위를 화면에서 다시 잡을 때 쓴다
                    "src": c.src, "y0": c.y0, "y1": c.y1,
                    "src_url": (url_for("cut_source", goods_no=p.goods_no, src=c.src)
                                if c.src >= 0 else ""),
                })
        edit_facts = _rd.facts if _rd else []

        # 편집 모드 HTML — 실제 페이지 모습 위에 블록 래퍼와 편집 칸 표시를 얹은 것.
        # 화면에서 드래그로 순서를 바꾸고 글자를 그 자리에서 고친다.
        from bebrave.smartstore.layout import BLOCK_KINDS, load_blocks, render_blocks
        _blocks = load_blocks(p.goods_no) if p.goods_no else None
        detail_edit = ""
        if _cs and _blocks:
            _by = {c.index: c for c in _cs.cuts}
            detail_edit = render_blocks(
                p, _cs, _blocks,
                lambda i: url_for("cut_image", goods_no=p.goods_no, filename=_by[i].filename)
                if i in _by else "",
                editable=True)
        block_kinds = BLOCK_KINDS

        # 추천 키워드 — content._demand_tags가 태그 채택 때 연관키워드 30개를 조회해놓고
        # 상위 3개만 쓰고 나머지와 검색량을 버리고 있었다(2026-09). 같은 함수를 다시 불러
        # 상품명용/태그용을 각각 뽑는다 — 하나로 합쳐 보여줬더니 "뭘 위한 목록인지 모르겠다"는
        # 피드백을 받았다(2026-09). 상품명엔 이미 들어간 단어를, 태그엔 이미 적용된 태그를
        # 빼고 "이 상품에 새로 써볼 만한 것"만 남긴다.
        from bebrave.smartstore.content import related_demand_keywords
        keyword_pool = related_demand_keywords(keyword, p, limit=20)

        opt_words_set = set(content["name"].split())
        name_recommend_keywords = [kw for kw in keyword_pool if kw.keyword not in opt_words_set][:10]

        applied_tags_set = set(content.get("tags", []))
        tag_recommend_keywords = [kw for kw in keyword_pool if kw.keyword not in applied_tags_set][:10]

        # 상품명 최적화 근거 — "동의어 제거+SEO 규칙 적용"이라고만 하고 실제로 뭘 지웠는지
        # 안 보여줬다(2026-09 발견). 원본과 최적화본의 단어 차이를 그대로 노출한다.
        raw_words = p.name.split()
        opt_words = content["name"].split()
        removed_words = [w for w in raw_words if w not in opt_words]

        # 11번가 경쟁가 분포 — 평균만 보면 소수의 초고가 상품에 끌려 판정이 왜곡된다
        # (실측: "우산" 평균 27,724원 vs 중앙값 12,660원, 2026-09). 최저/25%/중앙값/75%/최고를
        # 그대로 보여주고, "강함/보통/약함" 판정도 중앙값 기준으로 계산한다.
        from bebrave.sourcing.product_search import fetch_11st_products, price_competitiveness
        price_position = None
        try:
            competitors = fetch_11st_products(keyword, limit=20)
            price_position = price_competitiveness(sale_price, [c.price for c in competitors])
        except Exception:
            pass  # 11번가 조회 실패는 부가정보라 미리보기 자체를 막지 않는다

        # (카테고리는 위에서 이미 확정 — content 생성에 category_name으로 넘겨줬다)

        # 등록 전 항목 점검 — "지금 등록하면 어떤 칸이 비어서/더미로 나가는지"를 누르기
        # 전에 보여준다. 등록 후(상품 상세 화면)와 같은 audit_fields()를 그대로 써서
        # 등록 전/후를 같은 판정 기준으로 본다(2026-09). 카테고리·원산지 코드·A/S 연락처
        # 중 하나라도 안 갖춰지면 점검 패널 전체가 사유 한 줄만 보여주고 안 뜨던 문제가
        # 있었다(2026-09 발견) — build_request_body(strict=False)로 못 채운 항목은 빈 값
        # 그대로 두고 항상 전체 목록을 만든다. 진짜 등록(register_candidate)은 여전히
        # strict=True 기본값을 쓰므로 안전장치는 그대로 유지된다.
        audit_items, audit_error = [], ""
        try:
            from bebrave.smartstore.models import StoreProduct
            from bebrave.smartstore.register import build_request_body
            from bebrave.smartstore.origin import resolve_origin_code
            from bebrave.smartstore.field_audit import audit_fields
            from bebrave.config import MAX_LISTING_STOCK

            audit_token = get_access_token()
            origin_code = ""
            try:
                origin_code = resolve_origin_code(p.origin_country, audit_token)
            except Exception:
                pass  # 원산지 코드표 조회 실패 — 빈 값으로 두면 field_audit이 "비어 있음"으로 잡는다

            matched_attributes = []
            if cat_id:
                from bebrave.smartstore.attributes import fetch_category_attributes, match_attributes
                specs = fetch_category_attributes(cat_id, audit_token)
                if specs:
                    matched_attributes = match_attributes(specs, p.name, p.option_group_name, p.options)

            store_product = StoreProduct(
                name=content["name"], leaf_category_id=cat_id, sale_price=sale_price,
                stock_quantity=min(p.stock, MAX_LISTING_STOCK), detail_content=content["detail_content"],
                representative_image=p.main_image, optional_images=_audit_optional_images(p),
                supply_price=p.supply_price, margin_rate=margin.margin_rate,
                domemae_goods_no=p.goods_no, domemae_category=p.category, supplier=p.supplier,
                keyword=keyword, tags=content.get("tags", []), origin_country=p.origin_country,
                origin_code=origin_code, manufacturer=p.manufacturer, model=p.model,
                option_group_name=p.option_group_name, options=p.options,
                attributes=matched_attributes,
            )
            dry_run_body = build_request_body(store_product, status="SUSPENSION", access_token=audit_token, strict=False)
            audit_items = audit_fields(dry_run_body["originProduct"], domemae_goods_no=p.goods_no)
        except Exception as e:
            audit_error = f"등록 항목 점검 실패: {e}"

        ctx.update(
            raw_name=p.name,
            optimized_name=content["name"],
            goods_no=p.goods_no,
            tags=content.get("tags", []),
            name_recommend_keywords=name_recommend_keywords,
            tag_recommend_keywords=tag_recommend_keywords,
            removed_words=removed_words,
            price_position=price_position,
            detail_content=content["detail_content"],
            category_id=cat_id,
            category_name=cat_name,
            category_error=cat_error,
            sale_price=sale_price,
            supply_price=p.supply_price,
            margin_rate=margin.margin_rate,
            image_count=len(p.images),
            description_len=len(p.description),
            supply_matched=matched,
            detail_fetch_error=detail_fetch_error,
            audit_items=audit_items,
            audit_error=audit_error,
            audit_problem_count=sum(1 for i in audit_items if i.problem),
            claude_reason=__import__("bebrave.smartstore.claude_cli", fromlist=["x"]).unavailable_reason(),
            supply_category=p.category,
            image_usable=p.image_usable,
            generated_image_url=(
                url_for("generated_image", filename=request.args.get("generated_image"), _external=True)
                if request.args.get("generated_image") else ""
            ),
            has_gemini_key=bool(os.environ.get("GEMINI_API_KEY", "")),
            raw_image_url=p.main_image,
            seller_photos=[
                {"filename": c.filename,
                 "url": url_for("cut_image", goods_no=p.goods_no, filename=c.filename)}
                for c in seller_photos
            ],
            seller_note=seller_note,
            cut_based=bool(built),
            detail_basic=detail_basic,
            detail_ai=detail_ai,
            edit_cuts=edit_cuts,
            edit_facts=edit_facts,
            detail_edit=detail_edit,
            block_kinds=block_kinds,
            thumbs=_thumbs_view(p.goods_no),
        )
    except Exception as e:
        return _fail(f"미리보기 생성 실패: {e}")

    if is_modal:
        return render_template("preview_content.html", **ctx)
    return render_template("preview.html", **ctx)


@app.route("/candidates/register", methods=["POST"])
def register_candidate():
    if request.form.get("demo") == "1":
        # 샘플 화면에서 연 미리보기 — 버튼은 막아뒀지만 폼을 직접 보내도 등록되지 않게 한 번 더
        flash("샘플 화면에서는 등록하지 않습니다.", "error")
        return redirect(url_for("candidates_demo"))
    keyword = request.form.get("keyword", "")
    track = request.form.get("track", "")
    goods_no = request.form.get("goods_no", "")
    name_override = request.form.get("name_override", "").strip()
    live = request.form.get("live") == "on"

    # 오매칭 차단 — 실물 미확인이거나 자동판정이 불일치를 의심하면 등록 버튼을 눌러도
    # 막는다(2026-09 확정 방침: 자동 차단 + 사람 확인 둘 다). 후보 식별은 (keyword, track)
    # 조합 — supply_goods_no는 미조회 후보에서 빈 문자열이라 식별자로 못 쓴다.
    from bebrave.sourcing.models import registration_block_reason
    items = _load_json(SOURCING_LOG)
    cand = next((c for c in items if c.get("keyword") == keyword and c.get("track", "") == track), None)
    block_reason = (
        registration_block_reason(cand.get("supply_matched"), cand.get("human_confirmed", False))
        if cand is not None
        else "후보 정보를 찾을 수 없어 실물확인 여부를 확인할 수 없습니다"
    )
    if block_reason:
        flash(block_reason, "error")
        return redirect(url_for("candidates_preview", keyword=keyword, track=track, goods_no=goods_no))

    # 태그 5칸 + 판매가 — 미리보기에서 본 값을 그대로 등록에 반영한다. 예전엔 미리보기가
    # 보여준 태그·판매가가 폼에 실리지 않고 등록 시점에 다시 계산돼, 확인한 값과 실제
    # 등록물이 달라질 수 있었다(2026-09). 빈 입력칸은 무시하고, 태그를 하나도 안 채웠으면
    # None을 넘겨 파이프라인이 자동생성 태그를 그대로 쓰게 한다(override "없음"과
    # override "빈 리스트로 등록"을 구분).
    tags_input = [t.strip() for t in request.form.getlist("tag") if t.strip()]
    tags_override = tags_input or None
    sale_price_raw = request.form.get("sale_price_override", "").strip()
    sale_price_override = int(sale_price_raw) if sale_price_raw.isdigit() else None
    # 상세페이지 — 화면에서 도매 원본 탭을 골랐으면 저장된 AI 버전을 건너뛴다. AI 버전을
    # 골랐으면(또는 AI 버전이 없으면) pipeline이 저장된 버전을 쓰고, 없으면 원본으로 간다.
    use_basic = request.form.get("use_basic") == "1"
    # 즉시할인율 — 사람이 % 단위로 입력, 파이프라인엔 0~1 소수로 넘긴다.
    discount_raw = request.form.get("discount_percent", "").strip()
    try:
        discount_rate = max(0.0, min(1.0, float(discount_raw) / 100)) if discount_raw else 0.0
    except ValueError:
        discount_rate = 0.0
    # AI로 새로 만든 대표이미지 — 미리보기에서 생성했으면 그 URL을 그대로 등록에 반영한다.
    representative_image_override = request.form.get("representative_image_override", "").strip()
    # 등록 항목 점검 패널에서 고친 값 — "field_override:detailAttribute.brandName" 같은
    # 이름의 입력칸을 그대로 점(.) 경로 dict로 모은다(2026-09).
    field_overrides = {
        key[len("field_override:"):]: value.strip()
        for key, value in request.form.items()
        if key.startswith("field_override:")
    }

    buf = io.StringIO()
    try:
        from bebrave.smartstore.pipeline import run as pipeline_run

        # goods_no가 있으면(미리보기를 거친 경우) 정확히 그 상품만 등록 — 키워드 재검색으로
        # 미리본 것과 다른 상품이 뽑히는 걸 방지 (2026-07-13 발견된 미리보기/등록 불일치 수정)
        with redirect_stdout(buf):
            if goods_no:
                results = pipeline_run(
                    supply_id=goods_no,
                    dry_run=not live,
                    status="SUSPENSION",
                    name_override=name_override,
                    tags_override=tags_override,
                    sale_price_override=sale_price_override,
                    discount_rate=discount_rate,
                    representative_image_override=representative_image_override,
                    field_overrides=field_overrides,
                    skip_cuts=use_basic,
                )
            else:
                results = pipeline_run(
                    keyword=keyword,
                    dry_run=not live,
                    status="SUSPENSION",
                    name_override=name_override,
                    tags_override=tags_override,
                    sale_price_override=sale_price_override,
                    discount_rate=discount_rate,
                    representative_image_override=representative_image_override,
                    field_overrides=field_overrides,
                    skip_cuts=use_basic,
                )

        # 파이프라인은 중단 사유를 [건너뜀]/[오류]/[경고] 셋 중 하나로 찍는다 — 예전엔
        # [경고]만 찾아서(pipeline.py가 실제로 쓰는 건 대부분 [건너뜀]/[오류]) 사실상 항상
        # 빈 리스트였고, 사용자는 "마진 기준 미달이거나 카테고리 매칭 실패"라는 뭉뚱그린
        # 문구만 봤다. 실제 중단 사유는 이미 등록됨/마진 미달/원산지 코드 없음/부실 리스팅/
        # 카테고리 매칭 실패/이미지 업로드 실패/네이버 API 오류 등 6~7가지로 갈린다.
        reasons = _extract_pipeline_reasons(buf.getvalue())
        for r_msg in reasons:
            flash(r_msg, "error")

        if results:
            r = results[0]
            if live:
                flash(f"'{r.name}' 등록 완료 (판매중지 상태) — 상품ID {r.naver_product_id}", "success")
            else:
                flash(f"[미리보기] '{r.name}' — 판매가 {r.sale_price:,}원, 마진 {r.margin_rate:.1%} (실제 등록 안 함)", "success")
        elif not reasons:
            flash("등록 가능한 상품을 찾지 못했습니다 — 파이프라인이 사유를 남기지 않았습니다. 원본 로그를 확인하세요.", "error")
    except Exception as e:
        flash(f"등록 실패: {e}", "error")
    return redirect(url_for("candidates"))


@app.route("/generated_image/<filename>")
def generated_image(filename):
    """AI로 새로 만든 대표이미지를 서빙 — 등록(register_product) 시 이 URL을 다운로드해
    네이버 서버로 재업로드하므로, 로컬에서 접근 가능한 URL이 있어야 한다."""
    from flask import send_from_directory
    return send_from_directory(GENERATED_IMAGES_DIR, filename)


@app.route("/cut/<goods_no>/<filename>")
def cut_image(goods_no, filename):
    """상세 이미지를 쪼갠 컷을 미리보기 화면에 보여준다.

    이 주소는 우리 화면에서만 쓴다 — 등록할 때 pipeline이 같은 컷 파일을 네이버 서버로
    올리고 상세페이지의 주소를 전부 바꿔 끼우므로, 이 주소가 스마트스토어로 나가는 일은
    없다(만약 나가면 구매자 화면의 사진이 전부 깨진다)."""
    from flask import send_from_directory
    from bebrave.smartstore.cuts import CUTS_DIR
    return send_from_directory(CUTS_DIR / str(goods_no), filename)


@app.route("/cut_source/<goods_no>/<int:src>")
def cut_source(goods_no, src):
    """컷을 잘라낸 원본 이미지 — 범위를 다시 잡을 때 화면에 띄운다."""
    from flask import send_file
    from bebrave.smartstore.cuts import source_path
    p = source_path(goods_no, src)
    if not p.exists():
        return ("원본 없음", 404)
    return send_file(p)


@app.route("/candidates/recut", methods=["POST"])
def recut_candidate_cut():
    """컷이 잘못 잘렸을 때 원본에서 범위를 다시 잡는다(2026-09).

    여백 기준 분할이 아이콘 묶음 한가운데를 자르는 일이 있어서, 사람이 화면에서
    위아래를 끌어 범위를 고치면 그 자리에서 다시 잘라낸다. 같은 컷 번호를 덮어쓰므로
    그 컷을 쓰던 블록은 건드릴 필요가 없다."""
    from bebrave.smartstore.cuts import recut
    goods_no = request.form.get("goods_no", "")
    try:
        index = int(request.form.get("index", "-1"))
        y0 = int(float(request.form.get("y0", "0")))
        y1 = int(float(request.form.get("y1", "0")))
    except (TypeError, ValueError):
        return {"ok": False, "error": "값을 읽을 수 없습니다"}, 400
    try:
        cut = recut(goods_no, index, y0, y1)
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    if cut is None:
        return {"ok": False, "error": "컷을 찾을 수 없습니다 (원본이 없는 사진일 수 있습니다)"}, 404
    return {"ok": True, "index": cut.index, "height": cut.height,
            "url": url_for("cut_image", goods_no=goods_no, filename=cut.filename)}


def _audit_optional_images(p) -> list:
    """등록 전 점검에 넣을 추가이미지 — 등록(pipeline)과 같은 규칙: 화면 후보가 있으면 그것,
    없으면 정사각에 가까운 도매 사진."""
    from bebrave.smartstore.images import pick_product_shots
    from bebrave.smartstore.thumbs import extra_thumb_paths
    extras = extra_thumb_paths(p.goods_no)
    return [str(x) for x in extras] if extras is not None else pick_product_shots(p.images[1:])


def _thumbs_view(goods_no: str):
    """대표이미지 영역에 보낼 값. 후보를 만든 적이 없으면 None — 화면이 열리자마자 만든다."""
    from bebrave.smartstore.images import MIN_SOURCE_PX
    from bebrave.smartstore.thumbs import MAX_EXTRAS, load_thumbs
    ts = load_thumbs(goods_no) if goods_no else None
    if not ts:
        return None
    return {"min_px": MIN_SOURCE_PX,   # 등록이 막히는 원본 크기 — 화면 경고와 등록 판정이 같은 값을 쓴다
            "items":[{"id": t.id, "px": t.px, "text": t.text,
                       # 다시 만들면 같은 파일 이름을 덮어써서 브라우저가 옛 사진을 보여준다
                       "url": url_for("cut_image", goods_no=goods_no, filename=t.filename, v=ts.built_at)}
                      for t in ts.thumbs],
            "recommended": ts.recommended, "why": ts.why, "by_ai": ts.by_ai,
            "fallback_reason": ts.fallback_reason, "chosen": ts.chosen, "chosen_by": ts.chosen_by,
            "extras": ts.extra_ids(), "extras_by": ts.extras_by, "max_extras": MAX_EXTRAS}


@app.route("/candidates/thumbs", methods=["POST"])
def candidate_thumbs():
    """대표이미지 — 후보 만들기(build) → 추천(recommend) → 사람이 고르기(choose) (2026-09).

    추천은 자동 적용되지만, 사람이 추천과 다른 사진을 골라뒀으면 다시 추천받아도 그대로 둔다.
    고른 사진은 서버에 저장돼 등록(pipeline)이 그대로 대표이미지로 쓴다 — 등록 폼에 싣지 않는다."""
    goods_no = request.form.get("goods_no", "")
    step = request.form.get("step", "")
    if not goods_no:
        return {"ok": False, "error": "상품번호가 없습니다"}, 400
    try:
        from bebrave.smartstore import thumbs as th
        if step == "build":
            from bebrave.sourcing.domemae import fetch_product_detail
            p = fetch_product_detail(goods_no)
            ts = th.build_thumbs(goods_no, [u for u in p.images if u])
            if not ts.thumbs:
                return {"ok": False, "error": "글자 없이 상품만 보이는 사진을 찾지 못했습니다 — 지금 대표사진 그대로 등록됩니다"}
        elif step == "recommend":
            th.recommend(goods_no, request.form.get("name", ""))
        elif step == "choose":
            if not th.choose(goods_no, int(request.form.get("id", "-1"))):
                return {"ok": False, "error": "없는 후보입니다 — 새로고침하세요"}
        elif step == "extra":
            err = th.toggle_extra(goods_no, int(request.form.get("id", "-1")))
            if err:
                return {"ok": False, "error": err}
        elif step == "extras_reset":
            th.reset_extras(goods_no)
        else:
            return {"ok": False, "error": f"알 수 없는 단계: {step}"}, 400
        return {"ok": True, "view": _thumbs_view(goods_no)}
    except Exception as e:
        return {"ok": False, "error": f"대표이미지 처리 실패: {e}"}


@app.route("/candidates/ai_build", methods=["POST"])
def ai_build_candidate():
    """상세페이지 AI 버전을 만든다 — 화면이 단계(step)마다 한 번씩 불러 진행을 보여준다(2026-09).

    판독·문구는 이 Mac의 Claude Code(지금 쓰는 구독)로 한다. 못 쓰면 규칙으로 떨어지고,
    그 이유를 warn과 함께 돌려줘 화면에 표시한다.

    한 번에 돌리면 판독에 수십 초가 걸리는 동안 화면이 멈춘 것처럼 보인다. 단계는
    cuts(사진 자르기) → read(사진 판독) → compose(문구·구성). 사진 자르기는 캐시를
    쓰므로 다시 만들어도 사람이 고친 컷 범위는 그대로 남는다."""
    goods_no = request.form.get("goods_no", "")
    step = request.form.get("step", "")
    if not goods_no:
        return {"ok": False, "error": "상품번호가 없습니다"}, 400
    try:
        from bebrave.smartstore.cuts import build_cuts, load_cuts, load_seller_note
        from bebrave.smartstore.cut_reader import load_reading, read_cuts
        from bebrave.sourcing.domemae import fetch_product_detail

        if step == "cuts":
            p = fetch_product_detail(goods_no)
            cs = build_cuts(goods_no, [u for u in p.images if u])
            if not cs.cuts:
                return {"ok": False, "error": "사진 단위로 나눌 긴 상세 이미지가 없어 AI 버전을 만들 수 없습니다"}
            return {"ok": True, "summary": f"사진 {len(cs.cuts)}장으로 나눴습니다"}

        cs = load_cuts(goods_no)
        if not cs or not cs.cuts:
            return {"ok": False, "error": "잘라둔 사진이 없습니다 — 처음부터 다시 시도하세요"}

        if step == "read":
            from bebrave.smartstore.claude_cli import unavailable_reason
            why = unavailable_reason()
            prior = load_reading(goods_no)
            # 규칙으로 골라둔 결과가 남아 있으면 Claude를 쓸 수 있게 된 뒤에도 다시 읽지 않아
            # 공급사 자료 사진이 계속 섞였다(2026-09 발견) — 쓸 수 있으면 규칙 판독은 버린다.
            # 가치 판단(2026-09)이 없는 예전 판독도 다시 읽는다 — 안 그러면 초안이 원본 순서 그대로다
            r = read_cuts(cs, request.form.get("name", ""), request.form.get("category", ""),
                          force=not why and not (prior and prior.by_ai and prior.rated))
            used = sum(1 for c in cs.cuts if r.of(c.index).use)
            if not r.by_ai:
                return {"ok": True, "warn": True,
                        "summary": f"Claude를 못 써서 규칙으로 골랐습니다 — {r.fallback_reason or why or '저장된 규칙 판독'}"
                                   f" (쓸 사진 {used}장, 공급사 자료가 섞일 수 있음)"}
            recut = sum(1 for c in cs.cuts if r.of(c.index).recut)
            drafted = sum(1 for c in cs.cuts if r.in_draft(c.index))
            summary = (f"Claude가 읽었습니다 — 쓸 사진 {used}장 · 뺀 사진 {len(cs.cuts) - used}장. "
                       f"초안에는 {drafted}장(겹치거나 정보가 적은 {used - drafted}장은 사진 고르기에만)")
            if recut:
                summary += (f". 그중 {recut}장은 좋은 사진에 가격·공급사 정보가 섞여 뺐습니다"
                            " — 사진 고르기의 '범위 고치기'로 살릴 수 있습니다")
            return {"ok": True, "summary": summary}

        if step == "compose":
            from bebrave.smartstore.layout import blocks_used_cuts, draft_blocks, save_blocks, write_copy
            p = fetch_product_detail(goods_no)
            reading = load_reading(goods_no) or read_cuts(cs, p.name, p.category)
            copy = write_copy(p, reading.facts, load_seller_note(goods_no), force=True, reading=reading)
            blocks = draft_blocks(cs, reading, copy)
            if not blocks_used_cuts(blocks):
                return {"ok": False, "error": "쓸 수 있는 사진이 없어 페이지를 만들지 못했습니다"}
            save_blocks(goods_no, blocks)
            if copy.fallback_reason:
                return {"ok": True, "warn": True,
                        "summary": f"블록 {len(blocks)}개로 구성했습니다 — 문구는 기본 문구로 대체 ({copy.fallback_reason})"}
            return {"ok": True, "summary": f"블록 {len(blocks)}개로 페이지를 구성하고 문구를 썼습니다"}

        return {"ok": False, "error": f"알 수 없는 단계: {step}"}, 400
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/candidates/upload_images", methods=["POST"])
def upload_candidate_images():
    """판매자가 직접 찍은 사진과 메모를 상세페이지 재료로 넣는다(2026-09).

    같은 도매 상품을 파는 셀러가 전부 같은 사진을 쓰기 때문에, 실물 사진 한 장이
    경쟁자와 겹치지 않는 유일한 자산이 된다 — 그래서 배치할 때 도매 컷보다 먼저 쓴다."""
    from bebrave.smartstore.cuts import register_seller_image, save_seller_note

    keyword = request.form.get("keyword", "")
    track = request.form.get("track", "")
    goods_no = request.form.get("goods_no", "")
    back = {"keyword": keyword, "track": track, "goods_no": goods_no}

    if not goods_no:
        flash("상품번호가 없어 사진을 올릴 수 없습니다.", "error")
        return redirect(url_for("candidates_preview", **back))

    if "seller_note" in request.form:
        from bebrave.smartstore.cuts import CUTS_DIR, load_seller_note
        before = load_seller_note(goods_no)
        note = request.form.get("seller_note", "")
        save_seller_note(goods_no, note)
        if note.strip() != before.strip():
            # 메모가 바뀌면 저장된 문구를 버린다 — 안 그러면 고친 메모가 글에 반영되지 않는다.
            (CUTS_DIR / str(goods_no) / "copy.json").unlink(missing_ok=True)

    added, failed = 0, 0
    for f in request.files.getlist("photos"):
        if not f or not f.filename:
            continue
        try:
            if register_seller_image(goods_no, f.read()):
                added += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  [경고] 업로드 처리 실패 ({f.filename}): {e}")
            failed += 1

    if added:
        flash(f"사진 {added}장을 상세페이지 재료에 넣었습니다 — 아래 미리보기에 반영됐습니다."
              + (f" ({failed}장은 읽을 수 없어 건너뜀)" if failed else ""), "success")
    elif failed:
        flash(f"사진 {failed}장을 읽을 수 없었습니다 — 이미지 파일인지 확인해 주세요.", "error")
    else:
        flash("메모를 저장했습니다.", "success")
    return redirect(url_for("candidates_preview", **back))


@app.route("/candidates/drop_image", methods=["POST"])
def drop_candidate_image():
    """판매자가 올린 사진 한 장 빼기."""
    from bebrave.smartstore.cuts import drop_seller_cut
    goods_no = request.form.get("goods_no", "")
    back = {"keyword": request.form.get("keyword", ""), "track": request.form.get("track", ""),
            "goods_no": goods_no}
    if drop_seller_cut(goods_no, request.form.get("filename", "")):
        flash("사진을 뺐습니다.", "success")
    return redirect(url_for("candidates_preview", **back))


@app.route("/candidates/edit_blocks", methods=["POST"])
def edit_candidate_blocks():
    """상세페이지 화면에서 직접 손본 결과를 저장한다(2026-09).

    블록 목록을 JSON 한 덩어리로 받는다 — 화면에서 드래그로 순서를 바꾸고 글자를 그
    자리에서 고치기 때문에, 입력칸 단위로 받는 것보다 DOM 순서를 그대로 옮기는 쪽이
    어긋날 여지가 없다.
    """
    from bebrave.smartstore.layout import BLOCK_KINDS, Block, save_blocks

    goods_no = request.form.get("goods_no", "")
    back = {"keyword": request.form.get("keyword", ""), "track": request.form.get("track", ""),
            "goods_no": goods_no}
    if not goods_no:
        flash("상품번호가 없어 저장할 수 없습니다.", "error")
        return redirect(url_for("candidates_preview", **back))

    try:
        raw = json.loads(request.form.get("blocks_json", "[]"))
    except Exception as e:
        flash(f"구성을 읽을 수 없습니다: {e}", "error")
        return redirect(url_for("candidates_preview", **back))

    blocks = []
    for d in raw:
        kind = str(d.get("kind", ""))
        if kind not in BLOCK_KINDS:
            continue
        rows = []
        for row in (d.get("items") or []):
            cells = [str(x).strip() for x in (row if isinstance(row, list) else [row])]
            if any(cells):
                rows.append(cells)
        try:
            cut = int(d.get("cut", -1))
        except (TypeError, ValueError):
            cut = -1
        blocks.append(Block(kind=kind, title=str(d.get("title", "")).strip(),
                            body=str(d.get("body", "")).strip(), cut=cut, items=rows,
                            tone=str(d.get("tone", "light")) or "light"))

    if not blocks:
        flash("블록이 하나도 없어 저장하지 않았습니다.", "error")
        return redirect(url_for("candidates_preview", **back))

    save_blocks(goods_no, blocks)
    flash(f"상세페이지를 저장했습니다 (블록 {len(blocks)}개).", "success")
    return redirect(url_for("candidates_preview", **back))


@app.route("/candidates/generate_image", methods=["POST"])
def generate_candidate_image():
    """리메이크 후보의 대표이미지를 AI로 새로 만든다(2026-09) — 도매매 원본은 "참고"만
    하고 그대로 베끼지 않도록 프롬프트로 지시한다(image_ai.py 참고). GEMINI_API_KEY가
    없으면 이 라우트 자체를 화면에서 숨기고 대신 수동 생성 페이지로 안내한다 —
    아직 실제 API 호출로 검증되지 않았으니 처음 쓸 때는 결과 이미지를 반드시 확인할 것."""
    keyword = request.form.get("keyword", "")
    track = request.form.get("track", "")
    goods_no = request.form.get("goods_no", "")
    back = {"keyword": keyword, "track": track, "goods_no": goods_no}

    from bebrave.smartstore.image_ai import has_api_key, generate_product_image, build_remake_prompt
    if not has_api_key():
        flash("GEMINI_API_KEY가 설정되지 않아 AI 이미지 생성을 쓸 수 없습니다 — 수동 생성 링크를 이용하세요.", "error")
        return redirect(url_for("candidates_preview", **back))

    try:
        from bebrave.sourcing.domemae import fetch_product_detail
        import uuid
        p = fetch_product_detail(goods_no)
        prompt = build_remake_prompt(keyword, p.category)
        image_bytes = generate_product_image(p.main_image, prompt)

        GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.png"
        with open(GENERATED_IMAGES_DIR / filename, "wb") as f:
            f.write(image_bytes)

        flash("AI 이미지 생성 완료 — 미리보기 상세페이지에 반영됐습니다. 마음에 안 들면 다시 눌러 새로 만드세요.", "success")
        return redirect(url_for("candidates_preview", generated_image=filename, **back))
    except Exception as e:
        flash(f"AI 이미지 생성 실패: {e}", "error")
        return redirect(url_for("candidates_preview", **back))


# ── 상품 관리 (등록상품 + 재고동기화 + 판매추적 + 판매성과 통합) ──────────────────

PRODUCT_STATUS_CACHE = DATA_DIR / "product_status_cache.json"
PRODUCT_SYNC_CACHE = DATA_DIR / "product_sync_cache.json"
PRODUCT_PRICE_CACHE = DATA_DIR / "product_price_cache.json"

# 연동된 판매채널 — 지금은 스마트스토어뿐이지만 다른 채널이 늘어나면 여기 한 줄만 추가하면
# 상품 관리 표의 채널 아이콘·상세 이동에 자동으로 반영된다.
CHANNEL_META = {
    "smartstore": {"label": "스마트스토어", "abbr": "N", "color": "#03C75A"},
}


def _bulk_eligibility(is_suspended: bool, sync: dict, perf_status: str) -> list:
    """이 상품에 적용 가능한 일괄 액션 목록. 화면(버튼별 건수 표시)과 실행
    (products_bulk의 대상 필터)이 반드시 같은 기준을 쓰도록 판정을 여기 한 곳에 모은다 —
    두 곳에서 따로 판정하면 "2건 적용됨"이라 써놓고 실제로는 0건이 처리되는 일이 생긴다."""
    from bebrave.smartstore.sync import ACTION_OK, ACTION_MARGIN_WARN, ACTION_ERROR

    ok = ["suspend"]  # 판매중지는 조건 없음
    if is_suspended and (not sync or sync.get("action") == ACTION_OK):
        ok.append("resume")
    # 확인실패는 "도매처에 문제가 있다"가 아니라 "확인을 못 했다"이므로 자동 반영에서 뺀다 —
    # 연동이 끊긴 상태에서 일괄 반영을 누르면 멀쩡한 상품이 무더기로 내려간다.
    if sync and sync.get("action") not in (ACTION_OK, ACTION_ERROR):
        ok.append("apply_sync")
    if sync and sync.get("action") == ACTION_MARGIN_WARN and sync.get("suggested_price"):
        ok.append("apply_price")
    if perf_status.startswith("무판매"):
        ok.append("reoptimize")
    return ok


@app.route("/products")
def products_view():
    """등록상품(마스터) + 재고동기화(캐시) + 판매성과 + 판매추적 위험판정을 한 표로 합친다.
    도매매 재고는 방문마다 실시간 조회하지 않고 캐시만 읽는다 — "지금 확인" 버튼을 눌러야 갱신됨
    (등록상품 페이지의 PRODUCT_STATUS_CACHE 패턴 재사용, 2026-08 재설계)."""
    from bebrave.smartstore.sync import ACTION_OK
    from bebrave.tracker.products import ProductTracker
    from bebrave.config import AUTO_DELETE_MONTHS
    from bebrave.report import claim_counts_by_product, recent_order_counts, sales_tier

    registered = _load_json(REGISTERED_PRODUCTS)
    registered.reverse()
    perf_by_id = {p["naver_product_id"]: p for p in _performance_with_quality()}
    status_cache = _load_json(PRODUCT_STATUS_CACHE)
    status_by_id = {s["product_id"]: s for s in status_cache} if isinstance(status_cache, list) else {}
    sync_cache = _load_json(PRODUCT_SYNC_CACHE)
    sync_by_id = {s["naver_product_id"]: s for s in sync_cache} if isinstance(sync_cache, list) else {}
    sync_checked_at = sync_cache[0]["checked_at"] if sync_cache else None
    tracked_by_id = {p.product_id: p for p in ProductTracker(TRACKED_PRODUCTS).products}
    claims_by_id = claim_counts_by_product()
    recent_counts_by_id = recent_order_counts()
    price_cache = _load_json(PRODUCT_PRICE_CACHE)
    price_by_id = {r["naver_product_id"]: r for r in price_cache} if isinstance(price_cache, list) else {}

    rows = []
    for p in registered:
        pid = str(p.get("naver_product_id", ""))
        perf = perf_by_id.get(pid, {})
        sync = sync_by_id.get(pid)
        tracked = tracked_by_id.get(pid)
        live_status = status_by_id.get(pid)
        months_since_sold = tracked.months_since_sold() if tracked else None
        auto_delete_risk = months_since_sold is not None and months_since_sold >= AUTO_DELETE_MONTHS
        is_suspended = bool(live_status and live_status.get("status_type") == "SUSPENSION")
        sale_status = "확인필요" if not live_status else ("판매중지" if is_suspended else "판매중")

        order_count = perf.get("order_count", 0)
        recent_order_count = recent_counts_by_id.get(pid, 0)
        sales_status = sales_tier(recent_order_count)
        claims = claims_by_id.get(pid, {"RETURN": 0, "EXCHANGE": 0})
        return_rate = claims["RETURN"] / order_count if order_count else None
        exchange_rate = claims["EXCHANGE"] / order_count if order_count else None

        perf_status = perf.get("status", "")
        sync_action = sync["action"] if sync else ""

        reasons = []
        if is_suspended:
            reasons.append("네이버 판매중지")
        if sync and sync_action != ACTION_OK:
            reasons.append(sync_action)
        if perf_status.startswith("무판매"):
            reasons.append(perf_status)
        if auto_delete_risk:
            reasons.append(f"자동삭제 위험({months_since_sold}개월 미판매)")

        # 화면 필터용 태그 — 드롭다운 선택값이 이 목록에 있으면 그 행을 보여준다.
        filters = ["action"] if reasons else ["ok"]
        if is_suspended:
            filters.append("suspended")
        if sync_action in ("판매중지", "재고조정"):
            filters.append("stock")
        if sync_action == "마진경고":
            filters.append("margin")
        if perf_status.startswith("무판매"):
            filters.append("nosale")

        rows.append({
            "naver_product_id": pid,
            "name": p.get("name", ""),
            "sale_price": p.get("sale_price", 0),
            "margin_rate": p.get("margin_rate", 0),
            "supply_price": p.get("supply_price", 0),
            "registered_date": p.get("registered_date", ""),
            "live_status": live_status,
            "is_suspended": is_suspended,
            "sale_status": sale_status,
            "sync": sync,
            "return_count": claims["RETURN"],
            "exchange_count": claims["EXCHANGE"],
            "return_rate": return_rate,
            "exchange_rate": exchange_rate,
            "price_position": price_by_id.get(pid),
            "recent_order_count": recent_order_count,
            "sales_status": sales_status,
            "channels": (
                [{**CHANNEL_META["smartstore"], "code": "smartstore",
                  "modal_url": url_for("products_detail", product_id=pid)}]
                if pid else []
            ),
            "order_count": order_count,
            "revenue": perf.get("revenue", 0),
            "profit": perf.get("profit", 0),
            "uncertain_count": perf.get("uncertain_count", 0),
            "perf_status": perf.get("status", ""),
            "quality": perf.get("quality"),
            "name_change": perf.get("name_change"),
            "replacements": perf.get("replacements"),
            "auto_delete_risk": auto_delete_risk,
            "months_since_sold": months_since_sold,
            "days_since_registered": perf.get("days_since_registered"),
            "keyword": p.get("keyword", ""),
            "needs_action": bool(reasons),
            "reasons": reasons,
            "filters": filters,
            "eligible": _bulk_eligibility(is_suspended, sync, perf_status),
        })

    # 행마다 "문제 · 할 일"을 판정한다 — 헬스체크와 같은 기준(개당 순이익 하한·60일 무판매)
    from bebrave.config import MIN_ABS_PROFIT
    from bebrave.report import suggest_replacements
    from bebrave.report.product_triage import triage, VERDICT_ORDER, URGENT, REPLACE
    from bebrave.report.strategy import DEAD_DAYS, NEW_DAYS
    candidates = _load_json(SOURCING_LOG)
    for r in rows:
        if not r["replacements"]:
            r["replacements"] = suggest_replacements(r["keyword"], candidates, registered)
        r["replacements"] = [dict(c, url=url_for("candidates_preview", keyword=c.get("keyword", ""),
                                                 goods_no=c.get("supply_goods_no", ""), track=c.get("track", "")))
                             for c in r["replacements"] or []]
        r.update(triage(r, MIN_ABS_PROFIT, NEW_DAYS, DEAD_DAYS))
        stock = (r["sync"] or {}).get("supply_stock")
        if r["low_profit"] and "margin" not in r["filters"]:
            r["filters"].append("margin")
        if stock is not None and stock <= 10 and "stock" not in r["filters"]:
            r["filters"].append("stock")
        if r["verdict"] in (URGENT, REPLACE) and "action" not in r["filters"]:
            r["filters"] = [f for f in r["filters"] if f != "ok"] + ["action"]
        if (r["days_since_registered"] or 0) >= DEAD_DAYS and not r["recent_order_count"] and "nosale" not in r["filters"]:
            r["filters"].append("nosale")
    rows.sort(key=lambda r: VERDICT_ORDER[r["verdict"]])

    def _count(f):
        return sum(1 for r in rows if f in r["filters"])

    # 데이터 신선도 — 재고·판매상태가 하루 넘게 묵었으면 화면 맨 위에서 경고한다.
    # 한 달 묵은 "판매중지" 배지를 지금 상태로 믿고 판단하는 일을 막는다(2026-10).
    stale_days = None
    if sync_checked_at:
        try:
            stale_days = (datetime.now() - datetime.fromisoformat(sync_checked_at)).days
        except ValueError:
            pass
    return render_template("products.html", rows=rows, total=len(registered),
                           counts={k: _count(k) for k in ("action", "suspended", "margin", "nosale", "stock")},
                           sync_checked_at=sync_checked_at, stale_days=stale_days,
                           min_abs_profit=MIN_ABS_PROFIT, dead_days=DEAD_DAYS)


@app.route("/products/refresh_stock", methods=["POST"])
def products_refresh_stock():
    """"지금 확인" 버튼 — 도매매 재고·마진 대조(판정만, 자동반영 없음) +
    네이버 판매상태 전체조회(구 registered_check_all)를 한 번에 캐시에 저장."""
    from datetime import datetime
    from bebrave.smartstore.sync import sync_all, ACTION_OK

    checked_at = datetime.now().isoformat(timespec="minutes")
    try:
        results = sync_all(dry_run=True)
        cache = [{
            "naver_product_id": r.naver_product_id, "name": r.name, "action": r.action,
            "detail": r.detail, "new_stock": r.new_stock, "suggested_price": r.suggested_price,
            # 위탁판매에서 실제 판매 가능 수량은 도매처 재고다 — 판정과 무관하게 항상 저장.
            "supply_stock": r.supply_stock, "supply_price": r.supply_price,
            "checked_at": checked_at,
        } for r in results]
        PRODUCT_SYNC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRODUCT_SYNC_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        need = len([r for r in results if r.action != ACTION_OK])
        flash(f"재고·마진 확인 완료 — 조치 필요 {need}건 / 전체 {len(results)}건", "success")
    except Exception as e:
        flash(f"재고·마진 확인 실패: {e}", "error")

    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.product_status import fetch_product_statuses, find_status_mismatches

        token = get_access_token()
        statuses = fetch_product_statuses(token)
        status_cache = [{
            "product_id": s.product_id, "status_type": s.status_type,
            "display_status": s.status_type, "stock": s.stock_quantity,
            "checked_at": checked_at,
        } for s in statuses]
        PRODUCT_STATUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRODUCT_STATUS_CACHE, "w", encoding="utf-8") as f:
            json.dump(status_cache, f, ensure_ascii=False, indent=2)

        mismatches = find_status_mismatches(_load_json(REGISTERED_PRODUCTS), statuses)
        if mismatches:
            names = ", ".join(f"{m['name'][:16]}({m['live_status']})" for m in mismatches[:3])
            flash(f"네이버 판매상태 확인 완료 — {len(statuses)}건 중 {len(mismatches)}건 판매중 아님: {names}", "error")
        else:
            flash(f"네이버 판매상태 확인 완료 — {len(statuses)}건 모두 정상 판매중", "success")
    except Exception as e:
        flash(f"네이버 판매상태 확인 실패: {e}", "error")

    try:
        from bebrave.sourcing.product_search import fetch_11st_products, price_competitiveness

        registered = _load_json(REGISTERED_PRODUCTS)
        by_keyword = {}
        for p in registered:
            by_keyword.setdefault(p.get("keyword", ""), []).append(p)

        price_cache = []
        failed = 0
        for keyword, products in by_keyword.items():
            if not keyword:
                failed += len(products)
                continue
            try:
                competitors = fetch_11st_products(keyword, limit=20)
                prices = [c.price for c in competitors]
            except Exception:
                failed += len(products)
                continue
            for p in products:
                result = price_competitiveness(p.get("sale_price", 0), prices)
                price_cache.append({
                    "naver_product_id": p.get("naver_product_id", ""),
                    "label": result["label"], "market_avg": result["market_avg"],
                    "market_median": result["market_median"], "p25": result["p25"], "p75": result["p75"],
                    "min": result["min"], "max": result["max"],
                    "sample_size": result["sample_size"], "checked_at": checked_at,
                })
            time.sleep(0.3)  # 11번가 API 연속 호출 과부하 방지

        PRODUCT_PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRODUCT_PRICE_CACHE, "w", encoding="utf-8") as f:
            json.dump(price_cache, f, ensure_ascii=False, indent=2)
        msg = f"가격 경쟁력 확인 완료 — {len(price_cache)}건"
        if failed:
            msg += f" (키워드 없음/조회 실패 {failed}건 제외)"
        flash(msg, "success")
    except Exception as e:
        flash(f"가격 경쟁력 확인 실패: {e}", "error")

    return redirect(url_for("products_view"))


@app.route("/registered/status/<product_id>")
def registered_status(product_id):
    """로컬 JSON은 등록 당시 스냅샷이라 스마트스토어센터에서 직접 바꾸면 화면에 안 반영됨
    — 실시간 상태를 확인해서 목록에도 남도록 캐시에 저장 (2026-07-13 추가, 2026-07-31 캐시화)."""
    try:
        from datetime import datetime
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.register import fetch_registered_product
        token = get_access_token()
        info = fetch_registered_product(product_id, token)
        op = info.get("originProduct", {})
        scp = info.get("smartstoreChannelProduct", {})

        cache = _load_json(PRODUCT_STATUS_CACHE)
        cache = [s for s in cache if s.get("product_id") != product_id] if isinstance(cache, list) else []
        cache.append({
            "product_id": product_id,
            "status_type": op.get("statusType"),
            "display_status": scp.get("channelProductDisplayStatusType"),
            "stock": op.get("stockQuantity"),
            "checked_at": datetime.now().isoformat(timespec="minutes"),
        })
        PRODUCT_STATUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRODUCT_STATUS_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        flash(f"상품 {product_id} 실시간 상태를 갱신했습니다", "success")
    except Exception as e:
        flash(f"상태 확인 실패: {e}", "error")
    return redirect(url_for("products_view"))


@app.route("/products/detail/<product_id>")
def products_detail(product_id):
    """상품명 클릭 시 뜨는 자세히보기 모달 — 등록 기록 + 네이버 실시간 상세를 합쳐 보여준다."""
    from bebrave.smartstore.listing_quality import score_listing

    registered = _load_json(REGISTERED_PRODUCTS)
    record = next((p for p in registered if str(p.get("naver_product_id", "")) == product_id), None)
    if not record:
        return '<div class="flash flash-error">등록 기록을 찾을 수 없습니다.</div>', 404

    # 표에서 뺀 판매 실적·자동삭제 위험은 여기서 본다 — 계산은 계속 되고 있었는데
    # 재설계 때 화면에서만 사라져 있었다.
    from bebrave.tracker.products import ProductTracker
    from bebrave.config import AUTO_DELETE_MONTHS

    perf = next((p for p in _performance_with_quality()
                 if p["naver_product_id"] == product_id), {})
    tracked = next((p for p in ProductTracker(TRACKED_PRODUCTS).products
                    if p.product_id == product_id), None)
    months_since_sold = tracked.months_since_sold() if tracked else None

    ctx = {"record": record, "images": [], "tags": [], "detail_content": "",
           "stock": None, "status_type": None, "fetch_error": None,
           "perf": perf, "months_since_sold": months_since_sold,
           "auto_delete_months": AUTO_DELETE_MONTHS,
           "auto_delete_risk": months_since_sold is not None and months_since_sold >= AUTO_DELETE_MONTHS,
           "audit_items": [], "audit_problem_count": 0}
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.register import fetch_registered_product
        from bebrave.smartstore.field_audit import audit_fields
        token = get_access_token()
        info = fetch_registered_product(product_id, token)
        op = info.get("originProduct", {}) or {}
        images = op.get("images", {}) or {}
        img_list = []
        if images.get("representativeImage", {}).get("url"):
            img_list.append(images["representativeImage"]["url"])
        img_list += [i.get("url") for i in (images.get("optionalImages") or []) if i.get("url")]
        tags = ((op.get("detailAttribute", {}) or {}).get("seoInfo", {}) or {}).get("sellerTags") or []
        # "등록 항목 전체 보기" 패널 — 네이버 응답을 5가지만 꺼내 쓰고 나머지(원산지 코드·
        # A/S 연락처가 더미인지·고시 항목이 "상세페이지 참조"로 도배됐는지 등)를 버리고
        # 있었다(2026-09). 항목별 판정을 그대로 노출한다.
        audit_items = audit_fields(op, domemae_goods_no=record.get("domemae_goods_no", ""))
        ctx.update(
            images=img_list,
            tags=[t.get("text", "") if isinstance(t, dict) else str(t) for t in tags],
            detail_content=op.get("detailContent", "") or "",
            stock=op.get("stockQuantity"),
            status_type=op.get("statusType"),
            quality=score_listing(record, live_detail=info),
            audit_items=audit_items,
            audit_problem_count=sum(1 for i in audit_items if i.problem),
        )
    except Exception as e:
        ctx["fetch_error"] = str(e)
        ctx["quality"] = score_listing(record)

    return render_template("product_detail.html", **ctx)


@app.route("/products/edit_detail", methods=["POST"])
def products_edit_detail():
    """등록 후에도 상세설명·상품명·태그를 직접 고쳐 저장 — 지금까지 상품 상세 화면이
    네이버 실시간 데이터를 이미 받아와 보여주기만 하고, 고칠 수단이 없었다(2026-09).
    조회→수정→전체 재전송은 update_registered_product()가 이미 하고 있던 패턴을 그대로 쓴다."""
    pid = request.form.get("naver_product_id", "")
    name = request.form.get("name", "").strip()
    detail_content = request.form.get("detail_content", "").strip()
    tags = [t.strip() for t in request.form.getlist("tag") if t.strip()]

    registered = _load_json(REGISTERED_PRODUCTS)
    record = next((p for p in registered if str(p.get("naver_product_id", "")) == pid), None)
    if not record or not name or not detail_content:
        flash("수정할 상품을 찾을 수 없거나 상품명·상세설명이 비어 있습니다.", "error")
        return redirect(url_for("products_view"))

    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.register import update_registered_product

        def _mutate(body):
            body["originProduct"]["name"] = name
            body["originProduct"]["detailContent"] = detail_content
            body["originProduct"].setdefault("detailAttribute", {})["seoInfo"] = {
                "sellerTags": [{"text": t} for t in tags]
            }

        token = get_access_token()
        update_registered_product(pid, token, _mutate)

        # 로컬 원장은 name만 갖고 있다(models.py StoreProduct — detail_content/tags는
        # 저장 안 함, 상품 상세 화면이 매번 네이버에서 실시간으로 다시 읽어온다) — name만 갱신.
        record["name"] = name
        with open(REGISTERED_PRODUCTS, "w", encoding="utf-8") as f:
            json.dump(registered, f, ensure_ascii=False, indent=2)

        flash(f"'{name}' 상세설명·태그 수정 완료", "success")
    except Exception as e:
        flash(f"수정 실패: {e}", "error")

    return redirect(url_for("products_view"))


# ── 주문·발주 (주문확인 + 발주대기열 + 수동발주 통합, 탭: ready/dispatch/manual/history) ──

ORDER_TABS = ("ready", "dispatch", "failed", "manual", "history")

# 택배사별 배송조회 주소 — 송장번호만 있고 링크가 없어서 배송 문의가 올 때마다
# 번호를 복사해 택배사 사이트에 직접 붙여넣어야 했다. 발송처리 select와 같은 목록.
TRACKING_URLS = {
    "CJ대한통운": "https://trace.cjlogistics.com/next/tracking.html?wblNo={no}",
    "롯데택배": "https://www.lotteglogis.com/home/reservation/tracking/linkView?InvNo={no}",
    "우체국택배": "https://service.epost.go.kr/trace.RetrieveDomRigiTraceList.comm?sid1={no}",
    "한진택배": "https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillResult.do?mCode=MN038&schLang=KR&wblnumText2={no}",
    "로젠택배": "https://www.ilogen.com/web/personal/trace/{no}",
}


def _tracking_url(company: str, tracking_number: str) -> str:
    """모르는 택배사면 빈 문자열 — 엉뚱한 주소로 보내느니 링크를 안 거는 게 낫다."""
    tmpl = TRACKING_URLS.get((company or "").strip())
    if not tmpl or not tracking_number:
        return ""
    return tmpl.format(no=str(tracking_number).replace("-", "").strip())


def _annotate_orders(items: list) -> None:
    """큐 아이템에 화면용 파생값을 심는다(원본 파일은 안 건드림).

    결제 후 경과시간은 지금까지 헬스체크만 알고 주문 화면은 몰랐다 — 발송지연은
    스토어 페널티로 직결되므로 정작 주문을 처리하는 화면에서 보여야 한다.
    판정 기준(24시간)은 헬스체크와 같은 상수를 쓴다 — 두 화면이 다른 답을 내면 안 된다.
    """
    from bebrave.report.health import DISPATCH_DELAY_HOURS
    from bebrave.margin.calculator import calculate as calc_margin
    from bebrave.config import FREE_SHIPPING_THRESHOLD

    now = datetime.now()
    for i in items:
        try:
            elapsed = (now - datetime.fromisoformat(i.get("ordered_at", ""))).total_seconds() / 3600
        except ValueError:
            elapsed = None
        i["hours_since_order"] = elapsed
        i["is_delayed"] = elapsed is not None and elapsed >= DISPATCH_DELAY_HOURS
        i["tracking_url"] = _tracking_url(i.get("delivery_company", ""), i.get("tracking_number", ""))

        # 발주를 누르는 순간 "이 건 얼마 남는지"가 화면에 없었다 — 도매가만 보였다.
        unit_price = i.get("unit_price") or 0
        supply_unit = i.get("supply_unit_price")
        if unit_price and supply_unit:
            m = calc_margin(sale_price=unit_price, cost_price=supply_unit,
                            free_shipping=(unit_price >= FREE_SHIPPING_THRESHOLD))
            i["margin_amount"] = m.net_profit * i.get("quantity", 1)
            i["margin_rate"] = m.margin_rate
        else:
            i["margin_amount"] = None
            i["margin_rate"] = None


@app.route("/orders")
def orders():
    """탭 5개: 처리할 주문(ready) | 발송 대기(ordered) | 발주 실패(failed) |
    수동 발주(hold+직접입력) | 완료 이력(dispatched).
    큐 조회는 방문마다 최근 주문을 실시간 대조한다(구 발주 대기열 그대로 — 실제
    발주가 걸린 화면이라 캐시로 늦추지 않음). refresh_queue()가 취소 주문 강등까지
    같이 처리한다 — main.py purchase queue(주기 실행)와 같은 함수를 쓴다."""
    from bebrave.smartstore.purchase_queue import (
        load_queue, STATUS_READY, STATUS_HOLD, STATUS_ORDERED,
        STATUS_FAILED, STATUS_DISPATCHED,
    )

    tab = request.args.get("tab", "ready")
    if tab not in ORDER_TABS:
        tab = "ready"

    error = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.purchase_queue import refresh_queue
        items = refresh_queue(get_access_token())
    except Exception as e:
        error = str(e)
        items = load_queue()

    ready = [i for i in items if i["status"] == STATUS_READY]
    hold = [i for i in items if i["status"] == STATUS_HOLD]
    dispatch_wait = [i for i in items if i["status"] == STATUS_ORDERED]
    # 발주 실패는 지금까지 화면 어디에도 없었다 — 실패 순간 플래시 한 번 뜨고 끝이라,
    # 새로고침하면 돈이 걸린 그 주문이 어디 갔는지 확인할 방법이 없었다.
    failed = [i for i in items if i["status"] == STATUS_FAILED]
    done = [i for i in items if i["status"] == STATUS_DISPATCHED]
    done.sort(key=lambda i: i.get("updated_at", ""), reverse=True)
    # 오래된 주문이 위로 — 발송기한이 급한 것부터 처리하도록.
    ready.sort(key=lambda i: i.get("ordered_at", ""))
    dispatch_wait.sort(key=lambda i: i.get("ordered_at", ""))

    # 이머니 잔액 — ready 건이 있을 때만 확인(로그인 호출 비용이 있어 빈 큐에서는 생략).
    # 필요 금액은 도매가×수량 기준(실제 이머니에서 빠지는 값) — 판매가가 아니다.
    # supply_cost를 아이템에 심어두면 화면에서 체크 해제할 때마다 필요금액을 다시
    # 계산할 수 있다(안전버그B: 버튼 라벨이 서버 렌더 총량 그대로였던 문제).
    emoney = None
    emoney_error = None
    if ready:
        needed = 0
        for i in ready:
            supply_price = _lookup_supply_price(i["matched_goods_no"])
            i["supply_unit_price"] = supply_price
            i["supply_cost"] = supply_price * i["quantity"] if supply_price is not None else None
            if supply_price is not None:
                needed += supply_price * i["quantity"]
        try:
            from bebrave.sourcing.domemae_order import login, fetch_emoney_balance
            session_data = login()
            emoney = fetch_emoney_balance(session_data["sId"])
            emoney["needed"] = needed
            emoney["short"] = needed > emoney["cash"]
        except Exception as e:
            emoney_error = str(e)

    _annotate_orders(items)

    manual_prefill = {k: request.args.get(k, "") for k in
                       ("goods_no", "option_code", "qty", "receiver_name", "phone", "zipcode", "address1", "address2",
                        "shop_name", "delivery_memo", "product_order_id")}

    return render_template(
        "orders.html", tab=tab, ready=ready, hold=hold, dispatch_wait=dispatch_wait,
        failed=failed, done=done, error=error,
        emoney=emoney, emoney_error=emoney_error,
        **manual_prefill,
    )


@app.route("/orders/detail/<product_order_id>")
def orders_detail(product_order_id):
    """주문명 클릭 시 뜨는 자세히보기 모달 — 발주 큐에 저장돼 있지만 표에는 자리가
    없던 것들을 모은다(주문번호·판매가·수령인 연락처·매칭 방식·발주 결과).
    특히 매칭 방식은 엉뚱한 상품이 발주되는 사고의 원인이라 사람이 볼 수 있어야 한다."""
    from bebrave.smartstore.purchase_queue import load_queue

    pool = _demo_order_items() if product_order_id.startswith("DEMO-") else load_queue()
    item = next((i for i in pool if i.get("product_order_id") == product_order_id), None)
    if not item:
        return '<div class="flash flash-error">주문을 찾을 수 없습니다.</div>', 404

    if item.get("supply_unit_price") is None:
        item["supply_unit_price"] = _lookup_supply_price(item.get("matched_goods_no", ""))
    _annotate_orders([item])
    return render_template("order_detail.html", i=item)


@app.route("/orders/retry", methods=["POST"])
def orders_retry():
    """발주 실패 건을 발주 대기로 되돌린다. 실패 사유(이머니 부족·일시적 오류)를
    해결한 뒤 다시 발주 대상으로 올리는 용도 — 로컬 큐 상태만 바꾸므로 돈이 나가지 않는다."""
    from bebrave.smartstore.purchase_queue import load_queue, _save_queue, STATUS_READY, STATUS_FAILED

    ids = set(request.form.getlist("ids"))
    if not ids:
        flash("선택된 주문이 없습니다.", "error")
        return redirect(url_for("orders", tab="failed"))

    items = load_queue()
    n = 0
    for i in items:
        if i["product_order_id"] in ids and i["status"] == STATUS_FAILED:
            i["status"] = STATUS_READY
            i["hold_reason"] = ""
            n += 1
    if n:
        _save_queue(items)
        flash(f"{n}건을 발주 대기로 되돌렸습니다 — '처리할 주문' 탭에서 다시 발주하세요.", "success")
    else:
        flash("되돌릴 수 있는 실패 건이 없습니다.", "error")
    return redirect(url_for("orders", tab="ready"))


def _demo_order_items() -> list:
    """샘플 주문 목록 — 실제 발주 큐와 **같은 키 이름**을 쓴다. 예전에 택배사만 다른
    이름(company)을 써서, 실제 화면에서는 항상 빈칸인 버그가 샘플에서는 정상으로
    보였다. 상세 모달도 이 목록을 그대로 읽는다(샘플 정의가 한 곳에만 있도록)."""
    now = datetime.now()

    def _ago(hours):
        return (now - timedelta(hours=hours)).isoformat(timespec="minutes")

    return [
        {"product_order_id": "DEMO-Q1", "order_id": "DEMO-O1",
         "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "option_name": "", "quantity": 2, "unit_price": 3300, "matched_goods_no": "11013443",
         "matched_option_code": None, "matched_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "match_method": "id", "ordered_at": _ago(5),
         "orderer_name": "김철수", "orderer_tel": "010-1111-2222",
         "delivery_memo": "부재시 경비실에 맡겨주세요",
         "receiver_name": "김철수", "receiver_tel": "010-1111-2222", "receiver_zipcode": "06000",
         "receiver_address1": "서울시 강남구", "receiver_address2": "101호", "status": "ready", "hold_reason": "",
         "supply_unit_price": 2300, "supply_cost": 4600},
        # 결제 후 30시간 — 발송지연 배지가 실제로 어떻게 보이는지 확인하는 샘플
        {"product_order_id": "DEMO-Q2", "order_id": "DEMO-O2",
         "product_name": "우산 양산 양우산 자동우산  3단자동우산 우양산 골프우",
         "option_name": "", "quantity": 1, "unit_price": 4600, "matched_goods_no": "13187678",
         "matched_option_code": None, "matched_name": "우산 양산 양우산 자동우산  3단자동우산 우양산 골프우",
         "match_method": "name", "ordered_at": _ago(30),
         # 선물 주문 — 주문자와 수령인이 다른 경우(CS 연락은 주문자에게 해야 한다)
         "orderer_name": "최민호", "orderer_tel": "010-4444-5555",
         "receiver_name": "최지은", "receiver_tel": "010-7777-8888", "receiver_zipcode": "42000",
         "receiver_address1": "대구시 수성구", "receiver_address2": "", "status": "ready", "hold_reason": "",
         "supply_unit_price": 3190, "supply_cost": 3190},
        {"product_order_id": "DEMO-Q3", "order_id": "DEMO-O3",
         "product_name": "캠핑용 접이식 미니 테이블", "option_name": "카키",
         "quantity": 5, "unit_price": 13000, "matched_goods_no": "20000001", "matched_option_code": "02",
         "matched_name": "캠핑용 접이식 미니 테이블", "match_method": "id", "ordered_at": _ago(9),
         "receiver_name": "박민수", "receiver_tel": "010-5555-6666",
         "receiver_zipcode": "48000", "receiver_address1": "부산시 해운대구", "receiver_address2": "",
         "status": "hold", "hold_reason": "도매매 옵션 재고 부족 — '카키' 필요 5개, 재고 3개"},
        {"product_order_id": "DEMO-Q4", "order_id": "DEMO-O4",
         "product_name": "완전 다른 상품 XYZ", "option_name": "",
         "quantity": 1, "unit_price": 9900, "matched_goods_no": "", "matched_option_code": None,
         "matched_name": "", "match_method": "none", "ordered_at": _ago(12),
         "receiver_name": "한소망", "receiver_tel": "010-1212-3434",
         "receiver_zipcode": "61900", "receiver_address1": "광주시 서구", "receiver_address2": "",
         "status": "hold", "hold_reason": "도매매 상품 매칭 실패 — 수동 확인 필요"},
        {"product_order_id": "DEMO-Q5", "order_id": "DEMO-O5",
         "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "option_name": "", "quantity": 1, "unit_price": 3300, "status": "ordered",
         "match_method": "id", "ordered_at": _ago(28), "spent_amount": 2300,
         "hold_reason": "", "domemae_order_no": "OR9990001",
         "receiver_name": "정하늘", "receiver_tel": "010-2222-3333", "receiver_zipcode": "03000",
         "receiver_address1": "서울시 마포구", "receiver_address2": "202호"},
        {"product_order_id": "DEMO-Q6", "order_id": "DEMO-O6",
         "product_name": "우산 양산 양우산 자동우산 3단자동우산",
         "option_name": "", "quantity": 1, "unit_price": 4600, "matched_goods_no": "13187678",
         "matched_option_code": None, "matched_name": "우산 양산 양우산 자동우산",
         "match_method": "name", "ordered_at": _ago(40), "supply_cost": 3190,
         "receiver_name": "이서준", "receiver_tel": "010-3434-5656", "receiver_zipcode": "13500",
         "receiver_address1": "성남시 분당구", "receiver_address2": "", "status": "failed",
         "hold_reason": "도매매 이머니 잔액 부족 — 4,600원 필요", "updated_at": "2026-08-29"},
        {"product_order_id": "DEMO-Q7", "order_id": "DEMO-O7",
         "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱",
         "option_name": "", "quantity": 3, "unit_price": 3300, "status": "dispatched",
         "match_method": "id", "ordered_at": _ago(72), "spent_amount": 6900,
         "hold_reason": "", "domemae_order_no": "OR9990002", "tracking_number": "123456789012",
         "delivery_company": "CJ대한통운", "updated_at": "2026-08-28",
         "receiver_name": "오지훈", "receiver_tel": "010-9999-0000", "receiver_zipcode": "21000",
         "receiver_address1": "인천시 연수구", "receiver_address2": ""},
    ]


@app.route("/orders/demo")
def orders_demo():
    """주문이 아직 없거나 API가 안 될 때도 화면 구조(체크박스·일괄발주·보류사유·발송처리)를
    눈으로 확인할 수 있도록 가짜 데이터로 렌더링. 저장은 전혀 안 함."""
    demo_items = _demo_order_items()
    _annotate_orders(demo_items)
    tab = request.args.get("tab", "ready")
    if tab not in ORDER_TABS:
        tab = "ready"
    flash("샘플 데이터입니다 — 실제 주문이 아닙니다.", "success")
    ready = [i for i in demo_items if i["status"] == "ready"]
    hold = [i for i in demo_items if i["status"] == "hold"]
    dispatch_wait = [i for i in demo_items if i["status"] == "ordered"]
    failed = [i for i in demo_items if i["status"] == "failed"]
    done = [i for i in demo_items if i["status"] == "dispatched"]
    demo_emoney = {"total": 15000, "cash": 15000, "card": 0, "point": 320, "needed": 9890, "short": False}
    manual_prefill = {k: "" for k in
                       ("goods_no", "option_code", "qty", "receiver_name", "phone", "zipcode", "address1", "address2",
                        "shop_name", "delivery_memo", "product_order_id")}
    return render_template(
        "orders.html", tab=tab, ready=ready, hold=hold, dispatch_wait=dispatch_wait,
        failed=failed, done=done, error=None,
        emoney=demo_emoney, emoney_error=None, demo=True,
        **manual_prefill,
    )


@app.route("/orders/dispatch", methods=["POST"])
def orders_dispatch():
    product_order_id = request.form.get("product_order_id", "")
    tracking_number = request.form.get("tracking_number", "")
    company = request.form.get("company", "")
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import dispatch_order
        from bebrave.smartstore.purchase_queue import mark_dispatched

        token = get_access_token()
        dispatch_order(product_order_id, tracking_number, company, token)
        # 네이버에만 알리고 끝내면 로컬 큐가 계속 "발주완료"로 남아 발송 대기 탭에서
        # 사라지지 않고 완료 이력에도 안 올라간다 — 송장 자동확인 경로와 같은 마감 처리를 한다.
        mark_dispatched(product_order_id, tracking_number, company)
        flash(f"주문 {product_order_id} 발송처리 완료 (송장: {tracking_number})", "success")
    except Exception as e:
        flash(f"발송처리 실패: {e}", "error")
    return redirect(url_for("orders", tab="dispatch"))


# ── CS (반품·취소·상품문의) ────────────────────────────────────────────────

CS_PERIODS = (7, 30, 90)  # 조회 기간 선택지(일) — 화면 드롭다운과 서버가 같은 목록을 쓴다


@app.route("/cs")
def cs():
    from bebrave.config import FAST_SETTLEMENT_MAX_RETURN
    from bebrave.report import return_rate

    days = request.args.get("days", type=int) or 7
    if days not in CS_PERIODS:
        days = 7
    hours = days * 24

    claims = []
    error = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        token = get_access_token()
        # "RETURNED"/"CANCELED"는 무효한 값(400 오류) — 취소/반품/교환은 CLAIM_REQUESTED
        # 하나로 조회하고 claim_type으로 구분한다 (2026-08 수정).
        claims = fetch_new_orders(token, hours=hours, status_type="CLAIM_REQUESTED")
        claims.sort(key=lambda o: o.ordered_at, reverse=True)
        from bebrave.report import record_claims
        record_claims(claims)
    except Exception as e:
        error = str(e)

    inquiries = []
    inquiry_error = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.inquiries import fetch_inquiries
        token = get_access_token()
        inquiries = fetch_inquiries(token, days=days)
        inquiries.sort(key=lambda i: i.answered)  # 미답변(False) 먼저
    except Exception as e:
        inquiry_error = str(e)

    # 표에 뿌리는 건 실시간 조회분(최근 N일)뿐이라, 그동안 쌓아온 누적 원장은 이 화면에서
    # 한 번도 안 쓰였다 — 반품률은 빠른정산 자격이 걸린 수치라 CS 화면에 있어야 한다.
    rate = return_rate(days=30)
    return render_template("cs.html", claims=claims, hours=hours, days=days, error=error,
                            inquiries=inquiries, inquiry_error=inquiry_error,
                            return_stat=rate, return_limit=FAST_SETTLEMENT_MAX_RETURN,
                            periods=CS_PERIODS)


@app.route("/cs/demo")
def cs_demo():
    from bebrave.smartstore.orders import ProductOrder
    from bebrave.smartstore.inquiries import ProductInquiry

    demo_claims = [
        ProductOrder(product_order_id="DEMO-C1", order_id="DEMO-O1", product_name="우산 양산 양우산 자동우산",
                     option_name="", quantity=1, unit_price=4600, status="CANCELED", claim_type="CANCEL",
                     claim_reason="단순 변심", orderer_name="김철수", orderer_tel="010-1111-2222",
                     ordered_at="2026-08-15T09:00:00"),
        ProductOrder(product_order_id="DEMO-C2", order_id="DEMO-O2", product_name="실리콘주걱 대코 브라이트",
                     option_name="", quantity=2, unit_price=3300, status="RETURN", claim_type="RETURN",
                     claim_reason="상품 파손", orderer_name="이영희", orderer_tel="010-3333-4444",
                     ordered_at="2026-08-14T15:20:00"),
    ]
    demo_inquiries = [
        ProductInquiry(inquiry_id="DEMO-I1", product_name="실리콘주걱 대코 브라이트", content="재질이 어떻게 되나요?",
                        answered=False, questioner_name="박민수", created_date="2026-08-16T10:00:00"),
        ProductInquiry(inquiry_id="DEMO-I2", product_name="우산 양산 양우산 자동우산", content="색상 추가되나요?",
                        answered=True, questioner_name="최지은", created_date="2026-08-13T11:30:00",
                        answer_content="현재는 네이비 단일 색상만 판매 중입니다."),
    ]
    from bebrave.config import FAST_SETTLEMENT_MAX_RETURN
    flash("샘플 데이터입니다 — 실제 반품·문의가 아닙니다.", "success")
    return render_template("cs.html", claims=demo_claims, hours=168, days=7, error=None,
                            inquiries=demo_inquiries, inquiry_error=None, demo=True,
                            return_stat={"rate": 0.08, "claim_count": 2, "order_count": 25},
                            return_limit=FAST_SETTLEMENT_MAX_RETURN, periods=CS_PERIODS)


# ── 재고·가격 동기화 (판정은 /products 캐시로 보여주고, 반영 액션만 여기 남김) ──────────
# 전체 일괄 반영은 /products/bulk(action=apply_sync)로 통합됨(2단계) — 개별 가격 반영만 남음.

@app.route("/sync/apply_price", methods=["POST"])
def sync_apply_price():
    """마진경고 건의 권장가를 실제 판매가로 반영. sync.py는 판정만 하고 자동으로
    안 올리므로(노출순위 영향), 사람이 이 버튼을 눌러야만 바뀐다."""
    pid = request.form.get("naver_product_id", "")
    new_price = int(request.form.get("new_price", 0))
    registered = _load_json(REGISTERED_PRODUCTS)
    record = next((p for p in registered if str(p.get("naver_product_id", "")) == pid), None)
    if not record or not new_price:
        flash("적용 대상을 찾을 수 없습니다.", "error")
        return redirect(url_for("products_view"))

    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.register import update_registered_product
        from bebrave.margin.calculator import calculate as calc_margin

        def _mutate(body):
            body["originProduct"]["salePrice"] = new_price

        token = get_access_token()
        update_registered_product(pid, token, _mutate)

        old_price = record.get("sale_price", 0)
        record["sale_price"] = new_price
        m = calc_margin(sale_price=new_price, cost_price=record.get("supply_price", 0),
                         free_shipping=(new_price >= 30_000))
        record["margin_rate"] = round(m.margin_rate, 4)
        with open(REGISTERED_PRODUCTS, "w", encoding="utf-8") as f:
            json.dump(registered, f, ensure_ascii=False, indent=2)
        if PRODUCT_SYNC_CACHE.exists():
            PRODUCT_SYNC_CACHE.unlink()
        flash(f"판매가 변경: {old_price:,}원 → {new_price:,}원 (마진 {m.margin_rate:.1%})", "success")
    except Exception as e:
        flash(f"가격 변경 실패: {e}", "error")
    return redirect(url_for("products_view"))


@app.route("/sync/apply_one", methods=["POST"])
def sync_apply_one():
    """도매매 대조 결과(품절→중지, 재고조정) 한 건만 실제 반영. 캐시에 저장된
    판정을 SyncResult로 되살려 apply_result()에 그대로 넘긴다 — 전체 반영(sync_apply)과
    같은 판정 로직을 1건 단위로 쓰는 것뿐, 새 판정 로직은 만들지 않는다."""
    pid = request.form.get("naver_product_id", "")
    cache = _load_json(PRODUCT_SYNC_CACHE)
    entry = next((c for c in cache if str(c.get("naver_product_id", "")) == pid), None) if isinstance(cache, list) else None
    if not entry:
        flash("적용 대상을 찾을 수 없습니다 — '지금 확인'을 먼저 눌러주세요.", "error")
        return redirect(url_for("products_view"))

    try:
        from bebrave.smartstore.auth import get_access_token
        token = get_access_token()
        record = {"naver_product_id": pid, "name": entry.get("name", "")}
        msg = _apply_product_action("apply_sync", record, token, sync=entry)
        cache = [c for c in cache if str(c.get("naver_product_id", "")) != pid]
        with open(PRODUCT_SYNC_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        flash(f"{msg} 완료", "success")
    except Exception as e:
        flash(f"반영 실패: {e}", "error")
    return redirect(url_for("products_view"))


@app.route("/products/bulk", methods=["POST"])
def products_bulk():
    """상품 관리 표에서 체크한 여러 건을 한 번에 처리 — purchase_bulk_place()와 같은
    5단계(대상 필터 → dry-run 게이트 → 토큰 1회 획득 → 건별 try/except → 집계)를 쓴다.
    로컬 파일(등록원장·동기화 캐시)은 루프 밖에서 1회만 읽고 1회만 쓴다 — 건마다
    전체 파일을 로드/덮어쓰면 선택 건수가 늘수록 느려진다."""
    action = request.form.get("action", "")
    ids = set(request.form.getlist("ids"))
    live = request.form.get("live") == "on"
    if action not in ("apply_sync", "apply_price", "suspend", "resume", "reoptimize"):
        flash("알 수 없는 일괄 액션입니다.", "error")
        return redirect(url_for("products_view"))
    if not ids:
        flash("선택된 상품이 없습니다.", "error")
        return redirect(url_for("products_view"))

    registered = _load_json(REGISTERED_PRODUCTS)
    by_id = {str(p.get("naver_product_id", "")): p for p in registered}
    sync_cache = _load_json(PRODUCT_SYNC_CACHE)
    sync_by_id = {s["naver_product_id"]: s for s in sync_cache} if isinstance(sync_cache, list) else {}
    status_cache = _load_json(PRODUCT_STATUS_CACHE)
    status_by_id = {s["product_id"]: s for s in status_cache} if isinstance(status_cache, list) else {}
    perf_by_id = {p["naver_product_id"]: p for p in _performance_with_quality()}

    targets = []
    for pid in ids:
        record = by_id.get(pid)
        if not record:
            continue
        sync = sync_by_id.get(pid)
        is_suspended = bool(status_by_id.get(pid, {}).get("status_type") == "SUSPENSION")
        # 화면이 버튼 라벨에 띄운 건수와 같은 판정을 쓴다(_bulk_eligibility 한 곳에서만 판정).
        if action not in _bulk_eligibility(is_suspended, sync, perf_by_id.get(pid, {}).get("status", "")):
            continue
        targets.append((record, sync))

    if not targets:
        flash("선택한 건 중 이 액션을 적용할 수 있는 상품이 없습니다.", "error")
        return redirect(url_for("products_view"))

    if not live:
        if action == "reoptimize":
            from bebrave.smartstore.name_optimizer import optimize_name
            preview = ", ".join(
                f"{r['name'][:14]}→{optimize_name(r.get('keyword', ''), r['name'])[:14]}" for r, _ in targets[:5]
            )
        elif action == "apply_price":
            preview = ", ".join(f"{r['name'][:14]}→{s['suggested_price']:,}원" for r, s in targets[:5])
        else:
            preview = ", ".join(f"{r['name'][:16]}" for r, _ in targets[:5])
        more = f" 외 {len(targets)-5}건" if len(targets) > 5 else ""
        flash(f"[dry-run] {len(targets)}건 처리 예정 (실제 반영 안 함) — {preview}{more}. "
              f"실제로 반영하려면 '확인함' 체크 후 다시 실행하세요.", "success")
        return redirect(url_for("products_view"))

    try:
        from bebrave.smartstore.auth import get_access_token
        token = get_access_token()
    except Exception as e:
        flash(f"인증 실패 — 일괄 처리 중단: {e}", "error")
        return redirect(url_for("products_view"))

    ok, failed = 0, []
    applied_ids = set()
    for record, sync in targets:
        pid = str(record.get("naver_product_id", ""))
        try:
            _apply_product_action(action, record, token, sync=sync)
            ok += 1
            applied_ids.add(pid)
        except Exception as e:
            failed.append(f"{record.get('name', pid)[:16]}({e})")

    if action in ("reoptimize", "apply_price"):
        with open(REGISTERED_PRODUCTS, "w", encoding="utf-8") as f:
            json.dump(registered, f, ensure_ascii=False, indent=2)
    if action == "apply_sync" and applied_ids:
        sync_cache = [s for s in sync_cache if s.get("naver_product_id") not in applied_ids]
        with open(PRODUCT_SYNC_CACHE, "w", encoding="utf-8") as f:
            json.dump(sync_cache, f, ensure_ascii=False, indent=2)
    if action == "resume" and applied_ids:
        status_cache = [s for s in status_cache if s.get("product_id") not in applied_ids]
        with open(PRODUCT_STATUS_CACHE, "w", encoding="utf-8") as f:
            json.dump(status_cache, f, ensure_ascii=False, indent=2)

    msg = f"일괄 처리 완료 — 성공 {ok}건"
    if failed:
        msg += f", 실패 {len(failed)}건: " + "; ".join(failed[:3]) + (" 외" if len(failed) > 3 else "")
        _notify(f"[비브레이브] 상품 일괄처리 실패 {len(failed)}건\n" + "\n".join(f"- {f}" for f in failed[:5]))
    flash(msg, "success" if not failed else "error")
    return redirect(url_for("products_view"))


# ── 정산 (돈의 흐름) — 탭 3개(캘린더/대사/현금흐름), 로직은 각 탭 함수 그대로 ──────────

SETTLEMENT_TABS = ("calendar", "reconcile", "cashflow")


def _settlement_calendar_ctx():
    import calendar as _cal
    from bebrave.smartstore.auth import get_access_token
    from bebrave.smartstore.settlement import fetch_daily_settlements, fetch_vat_cases, vat_amount
    from bebrave.report.settlement_ledger import load_settlements

    today = date.today()
    selected_year = request.args.get("year", type=int) or today.year
    selected_month = request.args.get("month", type=int) or today.month
    if (selected_year, selected_month) > (today.year, today.month):
        selected_year, selected_month = today.year, today.month

    start = date(selected_year, selected_month, 1)
    end = date(selected_year, selected_month, _cal.monthrange(selected_year, selected_month)[1])

    daily = []
    error = None
    total_vat = None
    try:
        token = get_access_token()
        daily = fetch_daily_settlements(token, start, end)
        daily.sort(key=lambda d: d.settle_date)
        vat_cases = fetch_vat_cases(token, start, end)
        total_vat = sum(vat_amount(c) for c in vat_cases)
    except Exception as e:
        error = str(e)

    total_settle = sum(d.settle_amount for d in daily)
    total_benefit = sum(d.benefit_settle_amount for d in daily)

    case_records = load_settlements()
    case_total = sum(r["settle_amount"] for r in case_records)

    # 위탁판매는 도매가를 내가 먼저 결제하고 정산은 나중에 들어온다 — 들어올 돈만
    # 보여주면 지금 자금이 도는지 알 수 없다. 아직 정산 안 된 발주 지출을 같이 낸다.
    from bebrave.smartstore.purchase_queue import load_queue, STATUS_ORDERED, STATUS_DISPATCHED
    pending_spend = sum(i.get("spent_amount") or 0 for i in load_queue()
                        if i["status"] in (STATUS_ORDERED, STATUS_DISPATCHED))

    prev_month, prev_year = (12, selected_year - 1) if selected_month == 1 else (selected_month - 1, selected_year)
    next_month, next_year = (1, selected_year + 1) if selected_month == 12 else (selected_month + 1, selected_year)
    next_disabled = (next_year, next_month) > (today.year, today.month)

    return dict(
        daily=daily, error=error, total_settle=total_settle, total_benefit=total_benefit,
        total_vat=total_vat, case_total=case_total, case_count=len(case_records),
        pending_spend=pending_spend,
        selected_year=selected_year, selected_month=selected_month,
        prev_year=prev_year, prev_month=prev_month, next_year=next_year, next_month=next_month,
        next_disabled=next_disabled,
    )


def _settlement_reconcile_ctx():
    from bebrave.report.reconcile import reconcile, suggest_fee_rate
    from bebrave.config import ORDER_FEE, SALES_FEE_MAX, CS_RESERVE

    results = reconcile()
    # 주문ID 역순은 사람에게 아무 의미가 없다 — 최근 매출부터 보이게 날짜 역순으로.
    results.sort(key=lambda r: (r.get("date", ""), r["product_order_id"]), reverse=True)

    names = {str(p.get("naver_product_id", "")): p.get("name", "")
             for p in _load_json(REGISTERED_PRODUCTS)}
    assumed_rate = ORDER_FEE + SALES_FEE_MAX + CS_RESERVE
    for r in results:
        r["product_name"] = names.get(str(r.get("naver_product_id", "")), "")
        # 공제율(%)만 보여주면 "예상보다 더 떼였는지"를 사람이 암산해야 한다 — 차액을 같이 낸다.
        r["expected_deduction"] = round(r["revenue"] * assumed_rate)
        r["deduction_diff"] = r["deduction"] - r["expected_deduction"]

    suggestion = suggest_fee_rate(results)
    return dict(results=results, suggestion=suggestion, assumed_rate=assumed_rate)


def _settlement_cashflow_ctx():
    from bebrave.report import cash_events
    events = cash_events()
    return dict(events=events, ending_balance=events[-1]["balance"] if events else 0)


@app.route("/settlement")
def settlement_view():
    tab = request.args.get("tab", "calendar")
    if tab not in SETTLEMENT_TABS:
        tab = "calendar"
    ctx = {"calendar": _settlement_calendar_ctx, "reconcile": _settlement_reconcile_ctx,
           "cashflow": _settlement_cashflow_ctx}[tab]()
    return render_template("settlement.html", tab=tab, **ctx)


@app.route("/settlement/demo")
def settlement_demo():
    from bebrave.smartstore.settlement import DailySettlement
    from bebrave.report.reconcile import suggest_fee_rate
    from bebrave.report.cashflow import cash_events

    tab = request.args.get("tab", "calendar")
    if tab not in SETTLEMENT_TABS:
        tab = "calendar"
    today = date.today()

    if tab == "reconcile":
        from bebrave.config import ORDER_FEE, SALES_FEE_MAX, CS_RESERVE
        assumed_rate = ORDER_FEE + SALES_FEE_MAX + CS_RESERVE
        # 실제 대사 결과와 같은 키 구성을 쓴다 — 샘플만 다른 모양이면 검증 도구가 못 된다.
        demo_results = [
            {"product_order_id": "DEMO-R1", "revenue": 10000, "settle_amount": 8950,
             "deduction": 1050, "deduction_rate": 0.105, "settle_type": "NORMAL_SETTLE_ORIGINAL",
             "date": (today - timedelta(days=9)).isoformat(), "naver_product_id": "DEMO-P1",
             "settle_date": (today - timedelta(days=2)).isoformat(), "commission_amount": 1020,
             "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱"},
            {"product_order_id": "DEMO-R2", "revenue": 20000, "settle_amount": 17800,
             "deduction": 2200, "deduction_rate": 0.11, "settle_type": "QUICK_SETTLE_ORIGINAL",
             "date": (today - timedelta(days=8)).isoformat(), "naver_product_id": "DEMO-P2",
             "settle_date": (today - timedelta(days=6)).isoformat(), "commission_amount": 2180,
             "product_name": "우산 양산 양우산 자동우산 3단자동우산"},
            {"product_order_id": "DEMO-R3", "revenue": 15000, "settle_amount": 13350,
             "deduction": 1650, "deduction_rate": 0.11, "settle_type": "NORMAL_SETTLE_ORIGINAL",
             "date": (today - timedelta(days=7)).isoformat(), "naver_product_id": "DEMO-P1",
             "settle_date": "", "commission_amount": None,
             "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱"},
            {"product_order_id": "DEMO-R4", "revenue": 8000, "settle_amount": 7120,
             "deduction": 880, "deduction_rate": 0.11, "settle_type": "NORMAL_SETTLE_ORIGINAL",
             "date": (today - timedelta(days=5)).isoformat(), "naver_product_id": "",
             "settle_date": "", "commission_amount": None, "product_name": ""},
            {"product_order_id": "DEMO-R5", "revenue": 12000, "settle_amount": 10680,
             "deduction": 1320, "deduction_rate": 0.11, "settle_type": "QUICK_SETTLE_ORIGINAL",
             "date": (today - timedelta(days=4)).isoformat(), "naver_product_id": "DEMO-P2",
             "settle_date": (today - timedelta(days=1)).isoformat(), "commission_amount": 1300,
             "product_name": "우산 양산 양우산 자동우산 3단자동우산"},
        ]
        for r in demo_results:
            r["expected_deduction"] = round(r["revenue"] * assumed_rate)
            r["deduction_diff"] = r["deduction"] - r["expected_deduction"]
        demo_results.sort(key=lambda r: (r["date"], r["product_order_id"]), reverse=True)
        ctx = dict(results=demo_results, suggestion=suggest_fee_rate(demo_results),
                   assumed_rate=assumed_rate)
    elif tab == "cashflow":
        purchase_items = [
            {"status": "ordered", "updated_at": (today - timedelta(days=5)).isoformat(),
             "product_name": "실리콘주걱 대코 브라이트", "spent_amount": 4600},
            {"status": "dispatched", "updated_at": (today - timedelta(days=3)).isoformat(),
             "product_name": "우산 양산 양우산 자동우산", "spent_amount": 6380},
        ]
        settlements = [
            {"settle_date": (today - timedelta(days=1)).isoformat(), "settle_amount": 4100, "product_order_id": "PO-1"},
            {"settle_date": (today + timedelta(days=2)).isoformat(), "settle_amount": 5700, "product_order_id": "PO-2"},
        ]
        events = cash_events(purchase_items, settlements)
        ctx = dict(events=events, ending_balance=events[-1]["balance"] if events else 0)
    else:
        demo_daily = [
            DailySettlement(settle_date="2026-08-05", settle_amount=42000, benefit_settle_amount=-1200),
            DailySettlement(settle_date="2026-08-12", settle_amount=68000, benefit_settle_amount=-2000),
            DailySettlement(settle_date="2026-08-19", settle_amount=35000, benefit_settle_amount=0),
        ]
        ctx = dict(
            daily=demo_daily, error=None,
            total_settle=sum(d.settle_amount for d in demo_daily),
            total_benefit=sum(d.benefit_settle_amount for d in demo_daily),
            total_vat=8500, case_total=131400, case_count=6, pending_spend=10980,
            selected_year=today.year, selected_month=today.month,
            prev_year=today.year, prev_month=today.month, next_year=today.year, next_month=today.month,
            next_disabled=True,
        )

    flash("샘플 데이터입니다 — 실제 데이터가 아닙니다.", "success")
    return render_template("settlement.html", tab=tab, demo=True, **ctx)


@app.route("/settlement/sync_cases", methods=["POST"])
def settlement_sync_cases():
    """건별 정산 동기화 — /settle/case가 하루씩만 조회되는 API라 최근 N일을 반복 호출한다.
    호출 비용이 있어 자동이 아니라 사람이 누를 때만 실행(대사·수수료 실측 교정용)."""
    from bebrave.smartstore.auth import get_access_token
    from bebrave.smartstore.settlement import fetch_case_settlements_range
    from bebrave.report.settlement_ledger import upsert_case_settlements

    days = max(1, min(int(request.form.get("days", 14)), 31))  # 무제한 호출 방지
    try:
        token = get_access_token()
        end = date.today()
        start = end - timedelta(days=days - 1)
        cases = fetch_case_settlements_range(token, start, end)
        n = upsert_case_settlements(cases)
        flash(f"건별 정산 동기화 완료 — 최근 {days}일 조회, {n}건 반영", "success")
    except Exception as e:
        flash(f"건별 정산 동기화 실패: {e}", "error")
    return redirect(url_for("settlement_view", tab="calendar"))


# ── 판매 성과 (진단점수·상태 판정 — /products가 이 결과를 표에 합쳐서 보여줌) ──────────

def _performance_with_quality(live_quality: bool = False):
    """상품별 판매성과 + 리스팅 품질.

    live_quality=False(목록 기본)면 네트워크를 전혀 안 탄다. 품질 점수는 표에서
    빠지고 상세 모달로 옮겨갔는데(모달은 자체적으로 실시간 조회를 한다), 목록이
    계속 무판매 상품 수만큼 네이버를 호출하고 있어 방문마다 값 없는 비용을 냈다.
    """
    from bebrave.report import product_performance
    from bebrave.smartstore.listing_quality import score_listing

    results = product_performance()
    registered = _load_json(REGISTERED_PRODUCTS)
    by_id = {str(p.get("naver_product_id", "")): p for p in registered}

    # 전 상품 로컬 채점(상품명·마진, API 호출 없음).
    for p in results:
        record = by_id.get(p["naver_product_id"])
        if record:
            p["quality"] = score_listing(record)

    if live_quality:
        # 무판매 상품만 이미지·태그·상세설명까지 실시간 조회해 재채점 — 판매중/신규까지
        # 태우면 느려진다. 무판매는 정의상 소수라 비용이 자연히 제한된다.
        token = None
        for p in results:
            if not p["status"].startswith("무판매"):
                continue
            record = by_id.get(p["naver_product_id"])
            if not record:
                continue
            live_detail = None
            try:
                if token is None:
                    from bebrave.smartstore.auth import get_access_token
                    token = get_access_token()
                from bebrave.smartstore.register import fetch_registered_product
                live_detail = fetch_registered_product(p["naver_product_id"], token)
            except Exception:
                pass  # 실시간 조회 실패해도 로컬 채점만으로 진행
            p["quality"] = score_listing(record, live_detail)

    from bebrave.report.name_changes import load_name_changes, compare_before_after
    from bebrave.report import load_sales_orders, suggest_replacements
    changes = load_name_changes()
    if changes:
        sales_records = load_sales_orders()
        for p in results:
            p["name_change"] = compare_before_after(p["naver_product_id"], sales_records, changes)

    candidates = _load_json(SOURCING_LOG)
    for p in results:
        if p["status"].startswith("무판매"):
            record = by_id.get(p["naver_product_id"], {})
            p["replacements"] = suggest_replacements(record.get("keyword", ""), candidates, registered)

    return results


def _apply_product_action(action: str, record: dict, token: str, sync: dict = None) -> str:
    """건 1개에 실제 반영 액션 1개를 적용하고 사람이 읽을 결과 문장을 돌려준다.
    개별 버튼(판매중지/이름 재최적화/도매매 판정 반영)과 일괄 처리가 판정 로직을
    두 벌로 유지하지 않도록 이 함수 하나로 합친다. 실패하면 예외를 그대로 던지고
    호출쪽(개별 라우트 또는 일괄 루프)이 각자 방식으로 처리한다."""
    from bebrave.smartstore.register import update_registered_product
    pid = str(record.get("naver_product_id", ""))
    name = record.get("name", "")

    if action == "suspend":
        def _mutate(body):
            body["originProduct"]["statusType"] = "SUSPENSION"
            if "smartstoreChannelProduct" in body:
                body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "SUSPENSION"
        update_registered_product(pid, token, _mutate)
        return f"{name} 판매중지 완료"

    if action == "resume":
        def _mutate(body):
            body["originProduct"]["statusType"] = "SALE"
            if "smartstoreChannelProduct" in body:
                body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "ON"
        update_registered_product(pid, token, _mutate)
        return f"{name} 판매 재개 완료"

    if action == "apply_price":
        from bebrave.margin.calculator import calculate as calc_margin
        if not sync or not sync.get("suggested_price"):
            raise ValueError("권장가 정보 없음 — '지금 확인'을 먼저 실행하세요")
        new_price = sync["suggested_price"]

        def _mutate(body):
            body["originProduct"]["salePrice"] = new_price
        update_registered_product(pid, token, _mutate)
        old_price = record.get("sale_price", 0)
        record["sale_price"] = new_price
        m = calc_margin(sale_price=new_price, cost_price=record.get("supply_price", 0),
                         free_shipping=(new_price >= 30_000))
        record["margin_rate"] = round(m.margin_rate, 4)
        return f"{name} 판매가 {old_price:,}→{new_price:,}원"

    if action == "reoptimize":
        from bebrave.smartstore.name_optimizer import optimize_name
        from bebrave.report.name_changes import record_name_change
        old_name = name
        new_name = optimize_name(record.get("keyword", ""), old_name)
        if new_name == old_name:
            return f"{name} — 이미 최적화된 이름, 변경 없음"

        def _mutate(body):
            body["originProduct"]["name"] = new_name
        update_registered_product(pid, token, _mutate)
        record["name"] = new_name
        record_name_change(pid, old_name, new_name)
        return f"{old_name} → {new_name}"

    if action == "apply_sync":
        from bebrave.smartstore.sync import SyncResult, apply_result
        if not sync:
            raise ValueError("도매매 판정 캐시 없음 — '지금 확인'을 먼저 실행하세요")
        result = SyncResult(
            naver_product_id=sync["naver_product_id"], name=sync.get("name", name),
            action=sync["action"], detail=sync.get("detail", ""),
            new_stock=sync.get("new_stock"), suggested_price=sync.get("suggested_price"),
        )
        apply_result(result, token)
        return f"{name} — {sync['action']} 반영"

    raise ValueError(f"알 수 없는 액션: {action}")


@app.route("/performance/suspend", methods=["POST"])
def performance_suspend():
    pid = request.form.get("naver_product_id", "")
    registered = _load_json(REGISTERED_PRODUCTS)
    record = next((p for p in registered if str(p.get("naver_product_id", "")) == pid), {"naver_product_id": pid, "name": ""})
    try:
        from bebrave.smartstore.auth import get_access_token
        token = get_access_token()
        msg = _apply_product_action("suspend", record, token)
        flash(msg, "success")
    except Exception as e:
        flash(f"판매중지 실패: {e}", "error")
    return redirect(url_for("products_view"))


@app.route("/performance/resume", methods=["POST"])
def performance_resume():
    """네이버 판매중지 상태를 판매중으로 되돌린다 — 도매매 재고는 정상인데
    네이버만 중지된 경우(과거 대응·수동 조작 등)에 다시 파는 판단."""
    pid = request.form.get("naver_product_id", "")
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.register import update_registered_product

        def _mutate(body):
            body["originProduct"]["statusType"] = "SALE"
            if "smartstoreChannelProduct" in body:
                body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "ON"

        token = get_access_token()
        update_registered_product(pid, token, _mutate)
        cache = _load_json(PRODUCT_STATUS_CACHE)
        cache = [s for s in cache if s.get("product_id") != pid] if isinstance(cache, list) else []
        with open(PRODUCT_STATUS_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        flash(f"상품ID {pid} 판매 재개 완료", "success")
    except Exception as e:
        flash(f"판매 재개 실패: {e}", "error")
    return redirect(url_for("products_view"))


@app.route("/performance/reoptimize_name", methods=["POST"])
def performance_reoptimize_name():
    """무판매 상품의 이름을 name_optimizer로 다시 다듬어 즉시 반영.
    등록 원장(registered_products.json)의 name도 같이 갱신해야 발주큐/매출집계의
    이름 매칭 폴백이 새 이름 기준으로 계속 맞는다."""
    pid = request.form.get("naver_product_id", "")
    registered = _load_json(REGISTERED_PRODUCTS)
    record = next((p for p in registered if str(p.get("naver_product_id", "")) == pid), None)
    if not record:
        flash("등록 기록을 찾을 수 없습니다.", "error")
        return redirect(url_for("products_view"))

    old_name = record.get("name", "")
    try:
        from bebrave.smartstore.auth import get_access_token
        token = get_access_token()
        msg = _apply_product_action("reoptimize", record, token)
        with open(REGISTERED_PRODUCTS, "w", encoding="utf-8") as f:
            json.dump(registered, f, ensure_ascii=False, indent=2)
        if record.get("name") == old_name:
            flash(msg, "success")
        else:
            flash(f"상품명 변경: {msg} — 앞으로의 판매 실적을 이전과 비교합니다", "success")
    except Exception as e:
        flash(f"이름 재최적화 실패: {e}", "error")
    return redirect(url_for("products_view"))


# ── 판매추적 ──────────────────────────────────────────────────────────────

@app.route("/tracker/sync", methods=["POST"])
def tracker_sync():
    """13개월 자동삭제 위험 배지(/products)의 근거인 last_sold_date를 최근 주문으로 갱신.
    독립 판매추적 화면은 폐기됐지만(2026-08 재설계, 판매성과가 더 정확한 근거로 대체),
    이 배지가 계속 최신 데이터를 반영하도록 동기화 자체는 남겨둔다."""
    try:
        from bebrave.tracker.products import ProductTracker
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders

        t = ProductTracker(TRACKED_PRODUCTS)
        token = get_access_token()
        order_list = fetch_new_orders(token, hours=24 * 30)
        updated = t.sync_from_orders(order_list)
        t.save()
        flash(f"주문 {len(order_list)}건 조회 → {updated}개 상품 판매일 갱신", "success")
    except Exception as e:
        flash(f"동기화 실패: {e}", "error")
    return redirect(url_for("products_view"))


# ── 주간 리포트 ────────────────────────────────────────────────────────────

@app.route("/report")
def report():
    """주간리포트는 헬스체크로 합쳤다 — 옛 링크·즐겨찾기가 깨지지 않게 넘겨준다."""
    return redirect(url_for("health_view"))


# ── 마진 계산기 ────────────────────────────────────────────────────────────

@app.route("/margin", methods=["GET", "POST"])
def margin():
    """판매가로 마진을 보는 정방향과, 도매가로 권장 판매가를 뽑는 역방향 둘 다.
    역산은 소싱 파이프라인이 쓰는 estimate_sale_price를 그대로 재사용한다 —
    계산기와 실제 등록가가 다른 답을 내면 안 된다."""
    from bebrave.config import ORDER_FEE, SALES_FEE_MAX, CS_RESERVE, MIN_ABS_PROFIT

    result = None
    suggested = None
    mode = request.form.get("mode", "forward")
    error = None
    if request.method == "POST":
        from bebrave.margin.calculator import calculate as calc_margin, estimate_sale_price
        try:
            cost = int(request.form.get("cost") or 0)
            if mode == "reverse":
                if cost <= 0:
                    raise ValueError("도매가를 입력하세요")
                suggested = estimate_sale_price(cost)
                result = calc_margin(sale_price=suggested, cost_price=cost)
            else:
                price = int(request.form.get("price") or 0)
                if price <= 0 or cost <= 0:
                    raise ValueError("판매가와 도매가를 입력하세요")
                result = calc_margin(sale_price=price, cost_price=cost,
                                      free_shipping=request.form.get("free_shipping") == "on")
        except ValueError as e:
            # 숫자가 아닌 값이 들어오면 500으로 죽던 자리 — 화면에서 알려준다.
            error = "숫자를 입력하세요" if "invalid literal" in str(e) else str(e)

    return render_template("margin.html", result=result, mode=mode, suggested=suggested, error=error,
                            fee_rates={"order": ORDER_FEE, "sales": SALES_FEE_MAX, "cs": CS_RESERVE},
                            min_abs_profit=MIN_ABS_PROFIT)


# ── 도매매 발주 (실제 결제 — 확인 필수, 페이지는 /orders로 통합됨) ─────────────────

@app.route("/purchase/bulk_place", methods=["POST"])
def purchase_bulk_place():
    """발주 대기열에서 체크한 '바로 발주 가능' 건을 한 번에 처리.
    로그인(sId)은 배치당 한 번만 하고, 이후 상품마다 place_order를 반복 호출한다 —
    건마다 로그인하면 도매매 쪽에도 불필요한 부하를 준다. 하나가 실패해도 나머지는 계속 진행."""
    from bebrave.smartstore.purchase_queue import load_queue, mark_ordered, mark_failed, STATUS_READY
    from bebrave.sourcing.domemae_order import OrderItem, OrderOption, DeliveryInfo, login, place_order

    selected_ids = set(request.form.getlist("product_order_ids"))
    live = request.form.get("live") == "on"
    targets = [i for i in load_queue() if i["product_order_id"] in selected_ids and i["status"] == STATUS_READY]

    if not targets:
        flash("선택된 발주 대상이 없습니다.", "error")
        return redirect(url_for("orders", tab="ready"))

    if not live:
        preview = ", ".join(f"{i['product_name'][:16]}×{i['quantity']}" for i in targets[:5])
        more = f" 외 {len(targets)-5}건" if len(targets) > 5 else ""
        flash(f"[dry-run] {len(targets)}건 발주 예정 (실제 결제 안 함) — {preview}{more}. "
              f"실제로 넣으려면 '확인함' 체크 후 다시 실행하세요.", "success")
        return redirect(url_for("orders", tab="ready"))

    # 취소 주문 방어 — 큐에 담긴 뒤 주문이 취소됐을 수 있다. 발주 직전에 네이버
    # 실제 주문 상태를 다시 조회해, 그새 취소/반품/교환 클레임이 걸린 건은 여기서
    # 걸러낸다(2026-09 발견: 도매처로 돈이 나가고 물건이 배송되는 사고). 재확인
    # 자체가 실패하면 이머니 잔액 확인과 같은 방침으로 전체 발주를 중단한다 —
    # 확인 안 된 채로 강행하는 게 더 위험하다.
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_order_detail
        live_by_id = {
            o.product_order_id: o
            for o in fetch_order_detail([i["product_order_id"] for i in targets], get_access_token())
        }
    except Exception as e:
        flash(f"주문 상태 재확인 실패 — 안전을 위해 일괄 발주 중단: {e}", "error")
        return redirect(url_for("orders", tab="ready"))

    cancelled = [i for i in targets if live_by_id.get(i["product_order_id"]) and live_by_id[i["product_order_id"]].claim_type]
    if cancelled:
        from bebrave.smartstore.purchase_queue import mark_failed as _mark_cancelled
        for i in cancelled:
            _mark_cancelled(i["product_order_id"], "발주 직전 재확인 — 주문이 취소/반품/교환 요청됨, 발주 취소")
        targets = [i for i in targets if i not in cancelled]
    if not targets:
        flash(f"선택된 {len(cancelled)}건 전부 발주 직전 재확인에서 취소 상태로 확인돼 중단했습니다.", "error")
        return redirect(url_for("orders", tab="ready"))

    try:
        session_data = login()
    except Exception as e:
        flash(f"도매매 로그인 실패 — 일괄 발주 중단: {e}", "error")
        return redirect(url_for("orders", tab="ready"))

    # 안전버그A 수정: 이머니 부족은 버튼 disabled만으로는 못 막는다(자바스크립트가 꺼져
    # 있거나 값이 새로고침 전이면 뚫린다) — 실제 결제 직전에 서버가 다시 검증한다.
    needed = sum(
        (_lookup_supply_price(i["matched_goods_no"]) or 0) * i["quantity"] for i in targets
    )
    try:
        from bebrave.sourcing.domemae_order import fetch_emoney_balance
        cash = fetch_emoney_balance(session_data["sId"])["cash"]
        if needed > cash:
            flash(f"이머니 {needed - cash:,}원 부족 — 충전 후 다시 시도하세요 (필요 {needed:,}원 / 잔액 {cash:,}원)", "error")
            return redirect(url_for("orders", tab="ready"))
    except Exception as e:
        flash(f"이머니 잔액 확인 실패 — 안전을 위해 일괄 발주 중단: {e}", "error")
        return redirect(url_for("orders", tab="ready"))

    ok, failed = 0, []
    for i in targets:
        try:
            delivery = DeliveryInfo(
                name=i["receiver_name"], zipcode=i["receiver_zipcode"],
                address1=i["receiver_address1"], address2=i["receiver_address2"],
                phone=i["receiver_tel"], shop_name=i["matched_name"],
            )
            option = (OrderOption(option_code=i["matched_option_code"], quantity=i["quantity"])
                      if i.get("matched_option_code") else OrderOption(quantity=i["quantity"]))
            # 고객이 남긴 배송요청사항을 도매처로 넘긴다 — 도매처가 직배송하므로
            # 여기서 안 실으면 그 요청은 아무 데도 도달하지 않는다.
            item = OrderItem(goods_no=i["matched_goods_no"], options=[option],
                              delivery_message=(i.get("delivery_memo") or "")[:256])
            result = place_order([item], delivery, sId=session_data["sId"], dry_run=False)
            order_no = (result or {}).get("order", {}).get("orderNo", "?")
            supply_price = _lookup_supply_price(i["matched_goods_no"])
            spent = supply_price * i["quantity"] if supply_price is not None else None
            mark_ordered(i["product_order_id"], order_no, spent)
            ok += 1
        except Exception as e:
            mark_failed(i["product_order_id"], str(e))
            failed.append(f"{i['product_name'][:16]}({e})")

    msg = f"일괄 발주 완료 — 성공 {ok}건"
    if cancelled:
        msg += f", 취소 재확인으로 제외 {len(cancelled)}건"
    if failed:
        msg += f", 실패 {len(failed)}건: " + "; ".join(failed[:3]) + (" 외" if len(failed) > 3 else "")
        _notify(f"[비브레이브] 일괄발주 실패 {len(failed)}건\n" + "\n".join(f"- {f}" for f in failed[:5]))
    flash(msg, "success" if not failed else "error")
    return redirect(url_for("orders", tab="ready"))


@app.route("/purchase/sync_tracking", methods=["POST"])
def purchase_sync_tracking():
    """발주 완료건의 도매매 송장을 조회해 확보되면 바로 스마트스토어 발송처리까지 실행.
    getOrderView 응답 구조가 실주문으로 아직 검증 안 됐으니 결과를 항상 flash로 눈에 보이게 한다."""
    product_order_id = request.form.get("product_order_id", "")
    domemae_order_no = request.form.get("domemae_order_no", "")
    try:
        from bebrave.sourcing.domemae_order import login, fetch_order_tracking
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import dispatch_order
        from bebrave.smartstore.purchase_queue import mark_dispatched

        session_data = login()
        tracking = fetch_order_tracking(domemae_order_no, sId=session_data["sId"])
        if not tracking.get("tracking_number"):
            flash(f"주문 {product_order_id}: 아직 도매매 쪽 송장이 등록되지 않았습니다 — 잠시 후 다시 확인하세요.", "success")
            return redirect(url_for("orders", tab="dispatch"))

        token = get_access_token()
        dispatch_order(product_order_id, tracking["tracking_number"], tracking.get("company_name", ""), token)
        mark_dispatched(product_order_id, tracking["tracking_number"], tracking.get("company_name", ""))
        flash(f"주문 {product_order_id} 발송처리 완료 — {tracking.get('company_name','')} {tracking['tracking_number']}", "success")
    except Exception as e:
        flash(f"송장 확인/발송처리 실패: {e}", "error")
    return redirect(url_for("orders", tab="dispatch"))


@app.route("/purchase/place", methods=["POST"])
def purchase_place():
    goods_no = request.form.get("goods_no", "")
    option_code = request.form.get("option_code", "")
    qty = int(request.form.get("qty", 1))
    receiver_name = request.form.get("receiver_name", "")
    phone = request.form.get("phone", "")
    zipcode = request.form.get("zipcode", "")
    address1 = request.form.get("address1", "")
    address2 = request.form.get("address2", "")
    shop_name = request.form.get("shop_name", "")
    product_order_id = request.form.get("product_order_id", "")
    live = request.form.get("live") == "on"
    # 큐/이력에서 넘어온 경우 그 탭으로, 수동 발주 탭에서 직접 입력한 경우 수동 발주 탭으로 복귀
    return_tab = request.form.get("return_to", "manual")
    if return_tab not in ORDER_TABS:
        return_tab = "manual"

    try:
        from bebrave.sourcing.domemae_order import OrderItem, OrderOption, DeliveryInfo, login, place_order

        delivery = DeliveryInfo(
            name=receiver_name, zipcode=zipcode, address1=address1,
            address2=address2, phone=phone, shop_name=shop_name,
        )
        option = OrderOption(option_code=option_code, quantity=qty) if option_code else OrderOption(quantity=qty)
        item = OrderItem(goods_no=goods_no, options=[option],
                          delivery_message=request.form.get("delivery_memo", "")[:256])

        if not live:
            flash("[dry-run] 아래 내용으로 발주 요청이 구성됩니다 (실제 결제 안 함) — 실제 발주는 체크박스를 켜고 눌러야 함", "success")
            place_order([item], delivery, sId="", dry_run=True)
            return redirect(url_for("orders", tab=return_tab))

        # 취소 주문 방어 — 큐/이력에서 넘어온 건(product_order_id 있음)만 재확인 가능하다.
        # 수동 발주 탭에서 직접 입력한 건(네이버 주문과 무관)은 재확인 대상이 없다.
        if product_order_id:
            from bebrave.smartstore.auth import get_access_token
            from bebrave.smartstore.orders import fetch_order_detail
            live_orders = fetch_order_detail([product_order_id], get_access_token())
            live_order = live_orders[0] if live_orders else None
            if live_order and live_order.claim_type:
                from bebrave.smartstore.purchase_queue import mark_failed as _mark_cancelled
                _mark_cancelled(product_order_id, "발주 직전 재확인 — 주문이 취소/반품/교환 요청됨, 발주 취소")
                flash("발주 취소 — 재확인 결과 이 주문은 취소/반품/교환 요청된 상태입니다.", "error")
                return redirect(url_for("orders", tab=return_tab))

        session_data = login()
        result = place_order([item], delivery, sId=session_data["sId"], dry_run=False)
        order_no = (result or {}).get("order", {}).get("orderNo", "?")
        flash(f"발주 완료 — 주문번호 {order_no}", "success")
        if product_order_id:
            from bebrave.smartstore.purchase_queue import mark_ordered
            supply_price = _lookup_supply_price(goods_no)
            spent = supply_price * qty if supply_price is not None else None
            mark_ordered(product_order_id, order_no, spent)
    except Exception as e:
        if product_order_id:
            from bebrave.smartstore.purchase_queue import mark_failed
            mark_failed(product_order_id, str(e))
        flash(f"발주 실패: {e}", "error")
    return redirect(url_for("orders", tab=return_tab))


if __name__ == "__main__":
    # PORT/HOST가 설정되면(Render 등 외부 배포) 그걸 쓰고, 아니면 로컬 전용 기본값.
    port = int(os.environ.get("PORT", 5050))
    # Render 등은 PORT를 지정해서 실행하므로 그때만 0.0.0.0으로 바인딩 (로컬 실행 시엔 127.0.0.1 유지)
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    print(f"\nFriday — http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
