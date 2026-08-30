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
import io
import json
import os
import secrets
import sys
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
    """기본은 로컬 파일만 읽는 빠른 진단(deep=False). ?deep=1이면 검색량 계절성까지
    확인한다(데이터랩 API 호출 있음) — 방문마다 자동으로 돌리지 않는다."""
    from bebrave.report import check_store_health_macro
    deep = request.args.get("deep") == "1"
    result = check_store_health_macro(deep=deep)
    return render_template("health.html", env_status=_env_status(), **result)


@app.route("/health/demo")
def health_demo():
    demo_result = {
        "checked_at": datetime.now().isoformat(timespec="minutes"),
        "verdict": {"level": "주의", "message": "등록 상품 2개가 모두 판매중지 상태입니다 — 지금 스토어에서 살 수 있는 상품이 없습니다."},
        "trend": {
            "enough_sample": True, "window_days": 28,
            "this": {"revenue": 82000, "profit": 16200, "order_count": 9, "uncertain_count": 0},
            "prev": {"revenue": 104000, "profit": 21400, "order_count": 12, "uncertain_count": 0},
            "revenue_delta": -0.21, "profit_delta": -0.24, "order_delta": -0.25,
            "aov_this": 9111, "aov_prev": 8667, "aov_delta": 0.05,
        },
        "causes": [
            {"label": "판매 가능 상품 부족(추정)", "detail": "등록 2개 중 2개가 판매중지 — 매출 부재의 직접 원인일 수 있음", "link": "/products?tab=action"},
            {"label": "반품률", "detail": "최근 30일 28% (7건/25건) — 빠른정산 기준(20%) 기준 초과", "link": "/cs"},
            {"label": "실측 수수료율(추정)", "detail": "실측 11.2% vs 가정 9.5% (차이 +1.7%p, 표본 8건)", "link": "/settlement?tab=reconcile"},
        ],
        "opportunities": {"niche_count": 4, "remake_count": 3, "unclassified_count": 287,
                           "concentration": {"naver_product_id": "13599502225", "share": 0.68}},
        "vitals": {"fast_settlement_ok": False, "dispatch_delay_count": 1, "avg_margin_rate": 0.204,
                    "below_min_profit_count": 2, "cash_balance": -4600},
        "deep": False,
    }
    flash("샘플 데이터입니다 — 실제 진단이 아닙니다.", "success")
    return render_template("health.html", demo=True, env_status=_env_status(), **demo_result)


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

    order_items = []
    if pending_orders:
        order_items.append({"label": "발주 필요", "link": url_for("orders", tab="ready"), "n": pending_orders})
    dispatch_wait = len([i for i in load_queue() if i["status"] == STATUS_ORDERED])
    if dispatch_wait:
        order_items.append({"label": "발송 필요", "link": url_for("orders", tab="dispatch"), "n": dispatch_wait})
    groups.append({"name": "주문", "rows": order_items})

    # 품절·재고조정·마진붕괴·무판매 판정은 health.py(캐시 기반, deep=False)와 공유한다 —
    # 같은 판정을 두 곳에서 따로 하면 두 화면이 다른 답을 낼 수 있다.
    issues_by_category = {}
    for issue in check_store_health(deep=False):
        issues_by_category.setdefault(issue.category, []).append(issue)
    supply_n = len(issues_by_category.get("품절", [])) + len(issues_by_category.get("재고조정", []))
    margin_n = len(issues_by_category.get("마진붕괴", []))
    no_sale_n = len([p for p in product_performance(registered=registered) if p["status"].startswith("무판매")])

    # 네이버 판매중지는 그 자체로 할 일이 아니다 — 사람이 일부러 내렸을 수도 있다.
    # "도매매는 멀쩡한데 네이버만 막혀 있어 다시 팔 수 있는 것"만 실제로 할 일이다.
    from bebrave.smartstore.sync import ACTION_OK
    status_cache = _load_json(PRODUCT_STATUS_CACHE)
    sync_cache = _load_json(PRODUCT_SYNC_CACHE)
    registered_ids = {str(p.get("naver_product_id", "")) for p in registered}
    sync_by_id = {s.get("naver_product_id"): s for s in sync_cache} if isinstance(sync_cache, list) else {}
    resumable_n = 0
    if isinstance(status_cache, list):
        for s in status_cache:
            pid = s.get("product_id")
            if pid not in registered_ids or s.get("status_type") != "SUSPENSION":
                continue
            sync = sync_by_id.get(pid)
            if not sync or sync.get("action") == ACTION_OK:
                resumable_n += 1

    product_items = []
    if resumable_n:
        product_items.append({"label": "재개 가능", "link": url_for("products_view", tab="action"), "n": resumable_n})
    if supply_n:
        product_items.append({"label": "재고 반영", "link": url_for("products_view", tab="action"), "n": supply_n})
    if margin_n:
        product_items.append({"label": "마진경고", "link": url_for("products_view", tab="action"), "n": margin_n})
    if no_sale_n:
        product_items.append({"label": "무판매", "link": url_for("products_view", tab="action"), "n": no_sale_n})
    groups.append({"name": "상품", "rows": product_items})

    cs_items = []
    if returns_count:
        cs_items.append({"label": "반품·취소", "link": url_for("cs"), "n": returns_count})
    if inquiry_count:
        cs_items.append({"label": "미답변 문의", "link": url_for("cs"), "n": inquiry_count})
    groups.append({"name": "고객응대", "rows": cs_items})

    settle_items = []
    try:
        from bebrave.report.reconcile import reconcile, suggest_fee_rate
        s = suggest_fee_rate(reconcile())
        if s and abs(s["diff"]) > 0.01:
            settle_items.append({"label": f"수수료율 확인({s['diff']:+.1%}p)",
                                  "link": url_for("settlement_view", tab="reconcile"), "n": 1})
    except Exception:
        pass  # 표본 부족(5건 미만)이면 suggest_fee_rate가 None — 지어내지 않고 그냥 0건으로 둔다
    groups.append({"name": "정산", "rows": settle_items})

    for g in groups:
        g["count"] = sum(i["n"] for i in g["rows"])
    return groups


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
    this_month_returns = len([c for c in load_claims() if c.get("claimed_at", "").startswith(month_prefix)])

    prev_month, prev_year = (12, selected_year - 1) if selected_month == 1 else (selected_month - 1, selected_year)
    next_month, next_year = (1, selected_year + 1) if selected_month == 12 else (selected_month + 1, selected_year)
    next_disabled = (next_year, next_month) > (today.year, today.month)

    return render_template(
        "index.html",
        todo_groups=todo_groups, todo_total=todo_total, checked_at=checked_at,
        this_month=this_month, this_month_returns=this_month_returns, chart_series=chart_series,
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
    chart_series = month_series(demo_sales_records, today.year, today.month)
    this_month = {
        "revenue": sum(p["revenue"] for p in chart_series),
        "profit": sum(p["profit"] for p in chart_series),
        "order_count": sum(p["order_count"] for p in chart_series),
        "uncertain_count": sum(p.get("uncertain_count", 0) for p in chart_series),
    }

    demo_groups = [
        {"name": "주문", "count": 3, "rows": [
            {"label": "발주 필요", "link": url_for("orders_demo", tab="ready"), "n": 2},
            {"label": "발송 필요", "link": url_for("orders_demo", tab="dispatch"), "n": 1},
        ]},
        {"name": "상품", "count": 2, "rows": [
            {"label": "재개 가능", "link": url_for("products_view", tab="action"), "n": 2},
        ]},
        {"name": "고객응대", "count": 1, "rows": [
            {"label": "미답변 문의", "link": url_for("cs"), "n": 1},
        ]},
        {"name": "정산", "count": 0, "rows": []},
    ]

    flash("샘플 데이터입니다 — 오늘 할 일·주문·매출·반품 수치는 실제가 아닙니다.", "success")
    return render_template(
        "index.html",
        todo_groups=demo_groups, todo_total=6, checked_at=datetime.now().strftime("%H:%M"),
        this_month=this_month, this_month_returns=1, chart_series=chart_series,
        selected_year=today.year, selected_month=today.month,
        prev_year=today.year, prev_month=today.month, next_year=today.year, next_month=today.month,
        next_disabled=True,
        env_status=_env_status(), demo=True,
    )


# ── 발굴 후보 ─────────────────────────────────────────────────────────────

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
    from bebrave.config import TARGET_CATEGORIES
    items = _load_json(SOURCING_LOG)
    items.sort(key=lambda c: c.get("score", 0), reverse=True)

    counts = {"niche": 0, "remake": 0, "unclassified": 0, "hold": 0}
    for c in items:
        counts[_candidate_bucket(c)] += 1

    tab = request.args.get("tab", "niche")
    if tab not in counts:
        tab = "niche"
    unconfirmed_only = request.args.get("unconfirmed") == "1"

    filtered = [c for c in items if _candidate_bucket(c) == tab]
    if unconfirmed_only:
        filtered = [c for c in filtered
                    if c.get("supply_name") and not c.get("supply_matched") and not c.get("human_confirmed")]

    return render_template("candidates.html", candidates=filtered, target_categories=TARGET_CATEGORIES,
                            tab=tab, counts=counts, unconfirmed_only=unconfirmed_only)


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


@app.route("/candidates/discover", methods=["POST"])
def discover_scan():
    category = request.form.get("category", "주방용품")
    try:
        from bebrave.sourcing.analyzer import load_from_json, save_to_json, dedupe_by_supply
        existing = load_from_json(SOURCING_LOG)
        # (키워드, 트랙)으로 중복을 걸러야 한다 — 키워드만 보면 같은 키워드가 두
        # 트랙에서 다 나왔을 때 먼저 도는 트랙만 남고 리메이크 후보가 조용히 사라진다.
        existing_kw = {(c.keyword, c.track) for c in existing}
        added = 0

        if category == "all":
            from bebrave.sourcing.discover import scan_categories, to_product_candidates
            scores = scan_categories(limit=15)
            for score in scores:
                for c in to_product_candidates(score.results):
                    if (c.keyword, c.track) not in existing_kw:
                        existing.append(c)
                        existing_kw.add((c.keyword, c.track))
                        added += 1
            existing, removed_dupes = dedupe_by_supply(existing)
            save_to_json(existing, SOURCING_LOG)
            ranking = ", ".join(f"{s.category}({s.opportunity_density:.0%})" for s in
                                 sorted(scores, key=lambda s: s.opportunity_density, reverse=True))
            dupe_note = f", 동일상품 중복 {len(removed_dupes)}개 제거" if removed_dupes else ""
            flash(f"전체 카테고리 스캔 완료 — 신규 후보 {added}개{dupe_note}. 기회밀도: {ranking}", "success")
        else:
            from bebrave.sourcing.discover import discover, to_product_candidates
            result = discover(category=category, limit=15)
            for c in to_product_candidates(result):
                if (c.keyword, c.track) not in existing_kw:
                    existing.append(c)
                    existing_kw.add((c.keyword, c.track))
                    added += 1
            existing, removed_dupes = dedupe_by_supply(existing)
            save_to_json(existing, SOURCING_LOG)
            dupe_note = f", 동일상품 중복 {len(removed_dupes)}개 제거" if removed_dupes else ""
            flash(f"'{category}' 스캔 완료 — 신규 후보 {added}개 추가됨{dupe_note}", "success")
    except Exception as e:
        flash(f"스캔 실패: {e}", "error")
    return redirect(url_for("candidates"))


@app.route("/candidates/preview")
def candidates_preview():
    """상품명 최적화 · 태그 · 카테고리 · 마진을 실제 등록 전에 확인하는 미리보기.

    ?modal=1로 호출하면 발굴후보 목록에서 모달로 띄우기 위해 레이아웃 없이
    본문(preview_content.html)만 반환한다.
    """
    keyword = request.args.get("keyword", "")
    is_modal = request.args.get("modal") == "1"
    track = request.args.get("track", "")
    ctx = {"keyword": keyword, "modal": is_modal, "track": track}

    def _fail(message):
        if is_modal:
            return f'<div class="flash flash-error">{message}</div>', 200
        flash(message, "error")
        return redirect(url_for("candidates"))

    try:
        from bebrave.sourcing.domemae import search_products, fetch_product_detail, find_matching_product
        from bebrave.margin.calculator import calculate as calc_margin
        from bebrave.smartstore.content import generate_product_content
        from bebrave.smartstore.category import get_category_id, describe_category
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.pipeline import _decide_sale_price

        result = search_products(keyword, limit=10)
        if not result.products:
            return _fail(f"'{keyword}' 도매매 검색 결과 없음")

        # 최저가를 무조건 고르지 않고, discover()와 동일한 형태일치 검증을 거친다 —
        # 그냥 최저가를 집으면 재료/부자재가 완제품으로 둔갑하는 문제가 있었다(2026-08).
        p, matched = find_matching_product([keyword], result.products)
        if p is None:
            return _fail(f"'{keyword}' 도매매 매칭 후보 없음")
        if p.goods_no:
            try:
                p = fetch_product_detail(p.goods_no)
            except Exception:
                pass

        sale_price = _decide_sale_price(p.supply_price, p.retail_price)
        margin = calc_margin(sale_price=sale_price, cost_price=p.supply_price, free_shipping=(sale_price >= 30_000))
        content = generate_product_content(keyword, p, sale_price)

        cat_id, cat_name = "", ""
        try:
            token = get_access_token()
            cat_id = get_category_id(keyword, p.category, token)
            cat_name = describe_category(cat_id, token) if cat_id else "매칭 실패 — 수동 확인 필요"
        except Exception as e:
            cat_name = f"조회 실패: {e}"

        ctx.update(
            raw_name=p.name,
            optimized_name=content["name"],
            goods_no=p.goods_no,
            tags=content.get("tags", []),
            detail_content=content["detail_content"],
            category_id=cat_id,
            category_name=cat_name,
            sale_price=sale_price,
            supply_price=p.supply_price,
            margin_rate=margin.margin_rate,
            image_count=len(p.images),
            description_len=len(p.description),
            supply_matched=matched,
        )
    except Exception as e:
        return _fail(f"미리보기 생성 실패: {e}")

    if is_modal:
        return render_template("preview_content.html", **ctx)
    return render_template("preview.html", **ctx)


@app.route("/candidates/register", methods=["POST"])
def register_candidate():
    keyword = request.form.get("keyword", "")
    goods_no = request.form.get("goods_no", "")
    name_override = request.form.get("name_override", "").strip()
    live = request.form.get("live") == "on"
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
                )
            else:
                results = pipeline_run(
                    keyword=keyword,
                    dry_run=not live,
                    status="SUSPENSION",
                    name_override=name_override,
                )

        warnings = [line[7:].strip() for line in buf.getvalue().splitlines() if line.strip().startswith("  [경고]")]
        for w in warnings:
            flash(w, "error")

        if results:
            r = results[0]
            if live:
                flash(f"'{r.name}' 등록 완료 (판매중지 상태) — 상품ID {r.naver_product_id}", "success")
            else:
                flash(f"[미리보기] '{r.name}' — 판매가 {r.sale_price:,}원, 마진 {r.margin_rate:.1%} (실제 등록 안 함)", "success")
        else:
            flash("등록 가능한 상품을 찾지 못했습니다 (마진 기준 미달이거나 카테고리 매칭 실패)", "error")
    except Exception as e:
        flash(f"등록 실패: {e}", "error")
    return redirect(url_for("candidates"))


# ── 상품 관리 (등록상품 + 재고동기화 + 판매추적 + 판매성과 통합) ──────────────────

PRODUCT_STATUS_CACHE = DATA_DIR / "product_status_cache.json"
PRODUCT_SYNC_CACHE = DATA_DIR / "product_sync_cache.json"


def _bulk_eligibility(is_suspended: bool, sync: dict, perf_status: str) -> list:
    """이 상품에 적용 가능한 일괄 액션 목록. 화면(버튼별 건수 표시)과 실행
    (products_bulk의 대상 필터)이 반드시 같은 기준을 쓰도록 판정을 여기 한 곳에 모은다 —
    두 곳에서 따로 판정하면 "2건 적용됨"이라 써놓고 실제로는 0건이 처리되는 일이 생긴다."""
    from bebrave.smartstore.sync import ACTION_OK, ACTION_MARGIN_WARN

    ok = ["suspend"]  # 판매중지는 조건 없음
    if is_suspended and (not sync or sync.get("action") == ACTION_OK):
        ok.append("resume")
    if sync and sync.get("action") != ACTION_OK:
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

    registered = _load_json(REGISTERED_PRODUCTS)
    registered.reverse()
    perf_by_id = {p["naver_product_id"]: p for p in _performance_with_quality()}
    status_cache = _load_json(PRODUCT_STATUS_CACHE)
    status_by_id = {s["product_id"]: s for s in status_cache} if isinstance(status_cache, list) else {}
    sync_cache = _load_json(PRODUCT_SYNC_CACHE)
    sync_by_id = {s["naver_product_id"]: s for s in sync_cache} if isinstance(sync_cache, list) else {}
    sync_checked_at = sync_cache[0]["checked_at"] if sync_cache else None
    tracked_by_id = {p.product_id: p for p in ProductTracker(TRACKED_PRODUCTS).products}

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
            "sync": sync,
            "order_count": perf.get("order_count", 0),
            "revenue": perf.get("revenue", 0),
            "profit": perf.get("profit", 0),
            "uncertain_count": perf.get("uncertain_count", 0),
            "perf_status": perf.get("status", ""),
            "quality": perf.get("quality"),
            "name_change": perf.get("name_change"),
            "replacements": perf.get("replacements"),
            "auto_delete_risk": auto_delete_risk,
            "needs_action": bool(reasons),
            "reasons": reasons,
            "filters": filters,
            "eligible": _bulk_eligibility(is_suspended, sync, perf_status),
        })

    action_count = len([r for r in rows if r["needs_action"]])
    # 탭 대신 화면에서 검색어·상태를 즉시 걸러내는 필터로 처리한다(행 전부를 항상
    # 내려보내고 자바스크립트가 보여줄 것만 고른다) — 서버 왕복 없이 바로 반응한다.
    return render_template("products.html", rows=rows, total=len(registered),
                            action_count=action_count, ok_count=len(registered) - action_count,
                            sync_checked_at=sync_checked_at)


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


# ── 주문·발주 (주문확인 + 발주대기열 + 수동발주 통합, 탭: ready/dispatch/manual/history) ──

ORDER_TABS = ("ready", "dispatch", "failed", "manual", "history")


@app.route("/orders")
def orders():
    """탭 5개: 처리할 주문(ready) | 발송 대기(ordered) | 발주 실패(failed) |
    수동 발주(hold+직접입력) | 완료 이력(dispatched).
    큐 조회는 방문마다 최근 72시간 주문을 실시간 대조한다
    (구 발주 대기열 그대로 — 실제 발주가 걸린 화면이라 캐시로 늦추지 않음)."""
    from bebrave.smartstore.purchase_queue import (
        build_queue, load_queue, STATUS_READY, STATUS_HOLD, STATUS_ORDERED,
        STATUS_FAILED, STATUS_DISPATCHED,
    )

    tab = request.args.get("tab", "ready")
    if tab not in ORDER_TABS:
        tab = "ready"

    error = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        token = get_access_token()
        new_orders = fetch_new_orders(token, hours=24 * 3)
        items = build_queue(new_orders)
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

    manual_prefill = {k: request.args.get(k, "") for k in
                       ("goods_no", "option_code", "qty", "receiver_name", "phone", "zipcode", "address1", "address2",
                        "shop_name", "product_order_id")}

    return render_template(
        "orders.html", tab=tab, ready=ready, hold=hold, dispatch_wait=dispatch_wait,
        failed=failed, done=done, error=error,
        emoney=emoney, emoney_error=emoney_error,
        **manual_prefill,
    )


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


@app.route("/orders/demo")
def orders_demo():
    """주문이 아직 없거나 API가 안 될 때도 화면 구조(체크박스·일괄발주·보류사유·발송처리)를
    눈으로 확인할 수 있도록 가짜 데이터로 렌더링. 저장은 전혀 안 함."""
    demo_items = [
        {"product_order_id": "DEMO-Q1", "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "option_name": "", "quantity": 2, "unit_price": 3300, "matched_goods_no": "11013443",
         "matched_option_code": None, "matched_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "receiver_name": "김철수", "receiver_tel": "010-1111-2222", "receiver_zipcode": "06000",
         "receiver_address1": "서울시 강남구", "receiver_address2": "101호", "status": "ready", "hold_reason": "",
         "supply_cost": 6260},
        {"product_order_id": "DEMO-Q2", "product_name": "우산 양산 양우산 자동우산  3단자동우산 우양산 골프우",
         "option_name": "", "quantity": 1, "unit_price": 4600, "matched_goods_no": "13187678",
         "matched_option_code": None, "matched_name": "우산 양산 양우산 자동우산  3단자동우산 우양산 골프우",
         "receiver_name": "최지은", "receiver_tel": "010-7777-8888", "receiver_zipcode": "42000",
         "receiver_address1": "대구시 수성구", "receiver_address2": "", "status": "ready", "hold_reason": "",
         "supply_cost": 3630},
        {"product_order_id": "DEMO-Q3", "product_name": "캠핑용 접이식 미니 테이블", "option_name": "카키",
         "quantity": 5, "unit_price": 13000, "matched_goods_no": "20000001", "matched_option_code": "02",
         "matched_name": "캠핑용 접이식 미니 테이블", "receiver_name": "박민수", "receiver_tel": "010-5555-6666",
         "receiver_zipcode": "48000", "receiver_address1": "부산시 해운대구", "receiver_address2": "",
         "status": "hold", "hold_reason": "도매매 옵션 재고 부족 — '카키' 필요 5개, 재고 3개"},
        {"product_order_id": "DEMO-Q4", "product_name": "완전 다른 상품 XYZ", "option_name": "",
         "quantity": 1, "unit_price": 9900, "matched_goods_no": "", "matched_option_code": None,
         "matched_name": "", "receiver_name": "한소망", "receiver_tel": "010-1212-3434",
         "receiver_zipcode": "61900", "receiver_address1": "광주시 서구", "receiver_address2": "",
         "status": "hold", "hold_reason": "도매매 상품 매칭 실패 — 수동 확인 필요"},
        {"product_order_id": "DEMO-Q5", "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱 실리콘 이유식주걱",
         "option_name": "", "quantity": 1, "unit_price": 3300, "status": "ordered",
         "hold_reason": "", "domemae_order_no": "OR9990001"},
        {"product_order_id": "DEMO-Q6", "product_name": "우산 양산 양우산 자동우산 3단자동우산",
         "option_name": "", "quantity": 1, "unit_price": 4600, "matched_goods_no": "13187678",
         "matched_option_code": None, "matched_name": "우산 양산 양우산 자동우산",
         "receiver_name": "이서준", "receiver_tel": "010-3434-5656", "receiver_zipcode": "13500",
         "receiver_address1": "성남시 분당구", "receiver_address2": "", "status": "failed",
         "hold_reason": "도매매 이머니 잔액 부족 — 4,600원 필요", "updated_at": "2026-08-29"},
        {"product_order_id": "DEMO-Q7", "product_name": "실리콘주걱 대코 브라이트 미니볶음주걱",
         "option_name": "", "quantity": 3, "unit_price": 3300, "status": "dispatched",
         "hold_reason": "", "domemae_order_no": "OR9990002", "tracking_number": "123456789012",
         "company": "CJ대한통운", "updated_at": "2026-08-28"},
    ]
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
                        "shop_name", "product_order_id")}
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

        token = get_access_token()
        dispatch_order(product_order_id, tracking_number, company, token)
        flash(f"주문 {product_order_id} 발송처리 완료 (송장: {tracking_number})", "success")
    except Exception as e:
        flash(f"발송처리 실패: {e}", "error")
    return redirect(url_for("orders", tab="dispatch"))


# ── CS (반품·취소·상품문의) ────────────────────────────────────────────────

@app.route("/cs")
def cs():
    hours = int(request.args.get("hours", 24 * 7))
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
        inquiries = fetch_inquiries(token, days=max(1, hours // 24))
        inquiries.sort(key=lambda i: i.answered)  # 미답변(False) 먼저
    except Exception as e:
        inquiry_error = str(e)

    return render_template("cs.html", claims=claims, hours=hours, error=error,
                            inquiries=inquiries, inquiry_error=inquiry_error)


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
    flash("샘플 데이터입니다 — 실제 반품·문의가 아닙니다.", "success")
    return render_template("cs.html", claims=demo_claims, hours=168, error=None,
                            inquiries=demo_inquiries, inquiry_error=None, demo=True)


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

    prev_month, prev_year = (12, selected_year - 1) if selected_month == 1 else (selected_month - 1, selected_year)
    next_month, next_year = (1, selected_year + 1) if selected_month == 12 else (selected_month + 1, selected_year)
    next_disabled = (next_year, next_month) > (today.year, today.month)

    return dict(
        daily=daily, error=error, total_settle=total_settle, total_benefit=total_benefit,
        total_vat=total_vat, case_total=case_total, case_count=len(case_records),
        selected_year=selected_year, selected_month=selected_month,
        prev_year=prev_year, prev_month=prev_month, next_year=next_year, next_month=next_month,
        next_disabled=next_disabled,
    )


def _settlement_reconcile_ctx():
    from bebrave.report.reconcile import reconcile, suggest_fee_rate
    results = reconcile()
    results.sort(key=lambda r: r["product_order_id"], reverse=True)
    suggestion = suggest_fee_rate(results)
    return dict(results=results, suggestion=suggestion)


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
        demo_results = [
            {"product_order_id": "DEMO-R1", "revenue": 10000, "settle_amount": 8950,
             "deduction": 1050, "deduction_rate": 0.105, "settle_type": "NORMAL_SETTLE_ORIGINAL"},
            {"product_order_id": "DEMO-R2", "revenue": 20000, "settle_amount": 17800,
             "deduction": 2200, "deduction_rate": 0.11, "settle_type": "QUICK_SETTLE_ORIGINAL"},
            {"product_order_id": "DEMO-R3", "revenue": 15000, "settle_amount": 13350,
             "deduction": 1650, "deduction_rate": 0.11, "settle_type": "NORMAL_SETTLE_ORIGINAL"},
            {"product_order_id": "DEMO-R4", "revenue": 8000, "settle_amount": 7120,
             "deduction": 880, "deduction_rate": 0.11, "settle_type": "NORMAL_SETTLE_ORIGINAL"},
            {"product_order_id": "DEMO-R5", "revenue": 12000, "settle_amount": 10680,
             "deduction": 1320, "deduction_rate": 0.11, "settle_type": "QUICK_SETTLE_ORIGINAL"},
        ]
        ctx = dict(results=demo_results, suggestion=suggest_fee_rate(demo_results))
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
            total_vat=8500, case_total=131400, case_count=6,
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

def _performance_with_quality():
    from bebrave.report import product_performance
    from bebrave.smartstore.listing_quality import score_listing

    results = product_performance()
    registered = _load_json(REGISTERED_PRODUCTS)
    by_id = {str(p.get("naver_product_id", "")): p for p in registered}

    # 무판매 상품만 실시간 조회 — 판매중/신규 상품까지 매번 API를 태우면 방문마다 느려진다.
    # 무판매는 정의상 소수라 비용이 자연히 제한된다.
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
    from bebrave.report import weekly_summary
    return render_template("report.html", summary=weekly_summary())


# ── 마진 계산기 ────────────────────────────────────────────────────────────

@app.route("/margin", methods=["GET", "POST"])
def margin():
    result = None
    if request.method == "POST":
        from bebrave.margin.calculator import calculate as calc_margin
        result = calc_margin(
            sale_price=int(request.form.get("price", 0)),
            cost_price=int(request.form.get("cost", 0)),
            free_shipping=request.form.get("free_shipping") == "on",
        )
    return render_template("margin.html", result=result)


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
            item = OrderItem(goods_no=i["matched_goods_no"], options=[option])
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
        item = OrderItem(goods_no=goods_no, options=[option])

        if not live:
            flash("[dry-run] 아래 내용으로 발주 요청이 구성됩니다 (실제 결제 안 함) — 실제 발주는 체크박스를 켜고 눌러야 함", "success")
            place_order([item], delivery, sId="", dry_run=True)
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
