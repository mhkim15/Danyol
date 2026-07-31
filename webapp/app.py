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
from datetime import date, datetime
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


def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _find_registered_product(product_name: str) -> dict:
    """
    주문의 상품명으로 registered_products.json에서 도매매 상품번호를 역추적.
    스마트스토어 주문의 상품명은 등록시 넣은 이름 그대로 오므로(옵션 제외) 부분일치로 찾음
    (2026-07-13 추가 — 주문↔발주가 이전엔 아예 연결이 안 돼 있었음).
    """
    for p in _load_json(REGISTERED_PRODUCTS):
        name = p.get("name", "")
        if name and (name in product_name or product_name in name):
            return p
    return {}


# ── 홈 ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    from bebrave.tracker.products import ProductTracker
    from bebrave.report import load_sales_orders, sales_month_series

    candidates = _load_json(SOURCING_LOG)
    registered = _load_json(REGISTERED_PRODUCTS)
    recommended = sorted(
        (c for c in candidates if c.get("score", 0) >= 70),
        key=lambda c: c.get("score", 0), reverse=True,
    )

    tracker = ProductTracker(TRACKED_PRODUCTS)
    risky = tracker.auto_delete_risk()
    stale = tracker.stale_products()
    tracked_total = len(tracker.products)
    risky_count = len(risky)
    watch_count = len(stale) - risky_count
    normal_count = tracked_total - len(stale)

    checked_at = datetime.now().strftime("%H:%M")

    # 처리 대기 주문 — 최근 24시간 내 결제완료(PAYED)로 바뀐 뒤 아직 발송처리 안 된 건수.
    # 조회한 김에 매출 원장에도 바로 반영해서(record_sales_orders) 방문할 때마다
    # 자동으로 최신화되게 함 — 별도 "새로고침" 버튼/API 호출 불필요.
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

    # 반품·취소 — 최근 24시간 내 상태변경 건수 (별도 lastChangedType 조회라 실패해도 위 주문 조회엔 영향 없음)
    returns_count = None
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        token = get_access_token()
        returns_count = len(fetch_new_orders(token, hours=24, status_type="RETURNED"))
        returns_count += len(fetch_new_orders(token, hours=24, status_type="CANCELED"))
    except Exception:
        returns_count = None

    sales_records = load_sales_orders()
    today = date.today()
    selected_year = request.args.get("year", type=int) or today.year
    selected_month = request.args.get("month", type=int) or today.month
    if (selected_year, selected_month) > (today.year, today.month):
        selected_year, selected_month = today.year, today.month

    chart_series = sales_month_series(sales_records, selected_year, selected_month)
    is_current_month = (selected_year, selected_month) == (today.year, today.month)
    current_series = chart_series if is_current_month else sales_month_series(sales_records, today.year, today.month)
    this_month = {
        "revenue": sum(p["revenue"] for p in current_series),
        "profit": sum(p["profit"] for p in current_series),
        "order_count": sum(p["order_count"] for p in current_series),
    }

    prev_month, prev_year = (12, selected_year - 1) if selected_month == 1 else (selected_month - 1, selected_year)
    next_month, next_year = (1, selected_year + 1) if selected_month == 12 else (selected_month + 1, selected_year)
    next_disabled = (next_year, next_month) > (today.year, today.month)

    # (설정여부, 필수여부) — 카카오 알림·Claude API는 선택 기능이라 미설정이어도 경고색 안 씀
    env_status = {
        "도매매 (Open API)": (bool(os.environ.get("DOMEMAE_API_KEY")), True),
        "네이버 커머스 API": (bool(os.environ.get("NAVER_COMMERCE_CLIENT_ID")), True),
        "도매매 발주 (Private, 신규계정)": (bool(os.environ.get("DOMEMAE_USER_ID")), True),
        "카카오 알림 (선택)": (bool(os.environ.get("KAKAO_REST_API_KEY")), False),
        "Claude API (선택 — AI 상품명)": (bool(os.environ.get("ANTHROPIC_API_KEY")), False),
    }

    return render_template(
        "index.html",
        candidate_count=len(candidates),
        recommended_count=len(recommended),
        registered_count=len(registered),
        top_candidates=recommended[:3],
        tracked_total=tracked_total,
        normal_count=normal_count,
        watch_count=watch_count,
        risky_count=risky_count,
        risky_names=[p.name for p in risky[:3]],
        pending_orders=pending_orders,
        checked_at=checked_at,
        returns_count=returns_count,
        this_month=this_month,
        chart_series=chart_series,
        selected_year=selected_year,
        selected_month=selected_month,
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
        next_disabled=next_disabled,
        env_status=env_status,
    )


# ── 발굴 후보 ─────────────────────────────────────────────────────────────

@app.route("/candidates")
def candidates():
    items = _load_json(SOURCING_LOG)
    items.sort(key=lambda c: c.get("score", 0), reverse=True)
    return render_template("candidates.html", candidates=items)


@app.route("/candidates/discover", methods=["POST"])
def discover_scan():
    category = request.form.get("category", "주방용품")
    try:
        from bebrave.sourcing.analyzer import load_from_json, save_to_json
        existing = load_from_json(SOURCING_LOG)
        existing_kw = {c.keyword for c in existing}
        added = 0

        if category == "all":
            from bebrave.sourcing.discover import scan_categories, to_product_candidates
            scores = scan_categories(limit=15)
            for score in scores:
                for c in to_product_candidates(score.results):
                    if c.keyword not in existing_kw:
                        existing.append(c)
                        existing_kw.add(c.keyword)
                        added += 1
            save_to_json(existing, SOURCING_LOG)
            ranking = ", ".join(f"{s.category}({s.opportunity_density:.0%})" for s in
                                 sorted(scores, key=lambda s: s.opportunity_density, reverse=True))
            flash(f"전체 카테고리 스캔 완료 — 신규 후보 {added}개. 기회밀도: {ranking}", "success")
        else:
            from bebrave.sourcing.discover import discover, to_product_candidates
            result = discover(category=category, limit=15)
            for c in to_product_candidates(result):
                if c.keyword not in existing_kw:
                    existing.append(c)
                    existing_kw.add(c.keyword)
                    added += 1
            save_to_json(existing, SOURCING_LOG)
            flash(f"'{category}' 스캔 완료 — 신규 후보 {added}개 추가됨", "success")
    except Exception as e:
        flash(f"스캔 실패: {e}", "error")
    return redirect(url_for("candidates"))


@app.route("/candidates/preview")
def candidates_preview():
    """상품명 최적화 · 태그 · 카테고리 · 마진을 실제 등록 전에 확인하는 미리보기."""
    keyword = request.args.get("keyword", "")
    ctx = {"keyword": keyword}
    try:
        from bebrave.sourcing.domemae import search_products, fetch_product_detail
        from bebrave.margin.calculator import calculate as calc_margin
        from bebrave.smartstore.content import generate_product_content
        from bebrave.smartstore.category import get_category_id, describe_category
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.pipeline import _decide_sale_price

        result = search_products(keyword, limit=5)
        if not result.products:
            flash(f"'{keyword}' 도매매 검색 결과 없음", "error")
            return redirect(url_for("candidates"))

        p = result.cheapest or result.products[0]
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
        )
    except Exception as e:
        flash(f"미리보기 생성 실패: {e}", "error")
        return redirect(url_for("candidates"))

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


# ── 등록된 상품 ────────────────────────────────────────────────────────────

PRODUCT_STATUS_CACHE = DATA_DIR / "product_status_cache.json"


@app.route("/registered")
def registered():
    from bebrave.tracker.products import ProductTracker

    items = _load_json(REGISTERED_PRODUCTS)
    items.reverse()
    status_cache = _load_json(PRODUCT_STATUS_CACHE)
    status_by_id = {s["product_id"]: s for s in status_cache} if isinstance(status_cache, list) else {}
    tracked_ids = {p.product_id for p in ProductTracker(TRACKED_PRODUCTS).products}
    return render_template("registered.html", products=items, status_by_id=status_by_id, tracked_ids=tracked_ids)


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
    return redirect(url_for("registered"))


# ── 주문 ──────────────────────────────────────────────────────────────────

@app.route("/orders")
def orders():
    return render_template("orders.html", pairs=None, checked=False)


@app.route("/orders/demo")
def orders_demo():
    """
    커머스 API가 안 되는 상황(IP 미허용 등)에서도 화면 흐름을 눈으로 확인할 수 있도록
    가짜 주문 데이터로 렌더링. 실제 API 호출은 전혀 하지 않음 — bebrave 쪽 API 연동
    로직은 손대지 않고, 웹앱 화면단에만 있는 임시 확인용 기능 (2026-07-13 추가).
    """
    from bebrave.smartstore.orders import ProductOrder

    demo_orders = [
        ProductOrder(
            product_order_id="DEMO-0001", order_id="DEMO-ORDER-01",
            product_name="우산 양산 양우산 자동우산  3단자동우산 우양산 골프우",
            option_name="", quantity=1, unit_price=4600, status="PAYED",
            orderer_name="김철수", orderer_tel="010-1111-2222",
            receiver_name="김철수", receiver_tel="010-1111-2222",
            receiver_address="서울특별시 영등포구 국제금융로6길 30 101호",
            receiver_zipcode="07328", receiver_address1="서울특별시 영등포구 국제금융로6길 30",
            receiver_address2="101호", ordered_at="2026-07-13T09:12:00",
        ),
        ProductOrder(
            product_order_id="DEMO-0002", order_id="DEMO-ORDER-02",
            product_name="캠핑용 접이식 미니 테이블 야외 낚시 좌식상",
            option_name="블랙", quantity=2, unit_price=15900, status="PAYED",
            orderer_name="이영희", orderer_tel="010-3333-4444",
            receiver_name="이영희", receiver_tel="010-3333-4444",
            receiver_address="경기도 성남시 분당구 판교역로 235 5층",
            receiver_zipcode="13529", receiver_address1="경기도 성남시 분당구 판교역로 235",
            receiver_address2="5층", ordered_at="2026-07-13T10:03:00",
        ),
    ]
    pairs = [(o, _find_registered_product(o.product_name)) for o in demo_orders]
    flash("샘플 데이터입니다 — 실제 주문이 아닙니다. API 연결되면 '조회' 버튼으로 실제 데이터를 확인하세요.", "success")
    return render_template("orders.html", pairs=pairs, checked=True, demo=True)


@app.route("/orders/check", methods=["POST"])
def orders_check():
    hours = int(request.form.get("hours", 24))
    order_list = []
    try:
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders

        token = get_access_token()
        order_list = fetch_new_orders(token, hours=hours)
        if not order_list:
            flash(f"최근 {hours}시간 내 신규 주문 없음", "success")
    except Exception as e:
        flash(f"주문 조회 실패: {e}", "error")

    # 각 주문에 대해 도매매 상품번호를 미리 역추적해둠 — "발주하기" 링크에 사용
    pairs = [(o, _find_registered_product(o.product_name)) for o in order_list]
    return render_template("orders.html", pairs=pairs, checked=True)


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
    return redirect(url_for("orders"))


# ── 판매추적 ──────────────────────────────────────────────────────────────

@app.route("/tracker")
def tracker():
    from bebrave.tracker.products import ProductTracker
    t = ProductTracker(TRACKED_PRODUCTS)
    return render_template(
        "tracker.html",
        products=t.products,
        stale=t.stale_products(),
        risky=t.auto_delete_risk(),
    )


@app.route("/tracker/add", methods=["POST"])
def tracker_add():
    from datetime import date
    from bebrave.tracker.products import ProductTracker
    t = ProductTracker(TRACKED_PRODUCTS)
    t.add_or_update(
        request.form.get("product_id", ""),
        request.form.get("name", ""),
        request.form.get("registered_date") or date.today().isoformat(),
    )
    t.save()
    flash("추적 등록 완료", "success")
    return redirect(url_for(request.form.get("return_to", "tracker")))


@app.route("/tracker/sync", methods=["POST"])
def tracker_sync():
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
    return redirect(url_for("tracker"))


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


# ── 도매매 발주 (실제 결제 — 확인 필수) ──────────────────────────────────────

@app.route("/purchase")
def purchase():
    # 주문 페이지의 "이 주문 발주하기" 링크에서 쿼리 파라미터로 값을 넘겨받아 폼을 채움
    prefill = {k: request.args.get(k, "") for k in
               ("goods_no", "qty", "receiver_name", "phone", "zipcode", "address1", "address2", "shop_name")}
    return render_template("purchase.html", **prefill)


@app.route("/purchase/place", methods=["POST"])
def purchase_place():
    goods_no = request.form.get("goods_no", "")
    qty = int(request.form.get("qty", 1))
    receiver_name = request.form.get("receiver_name", "")
    phone = request.form.get("phone", "")
    zipcode = request.form.get("zipcode", "")
    address1 = request.form.get("address1", "")
    address2 = request.form.get("address2", "")
    shop_name = request.form.get("shop_name", "")
    live = request.form.get("live") == "on"
    # 주문 카드에서 바로 발주한 경우 주문 페이지로, 발주 화면에서 보낸 경우 발주 화면으로 복귀
    return_to = request.form.get("return_to", "purchase")

    try:
        from bebrave.sourcing.domemae_order import OrderItem, OrderOption, DeliveryInfo, login, place_order

        delivery = DeliveryInfo(
            name=receiver_name, zipcode=zipcode, address1=address1,
            address2=address2, phone=phone, shop_name=shop_name,
        )
        item = OrderItem(goods_no=goods_no, options=[OrderOption(quantity=qty)])

        if not live:
            flash("[dry-run] 아래 내용으로 발주 요청이 구성됩니다 (실제 결제 안 함) — 실제 발주는 체크박스를 켜고 눌러야 함", "success")
            place_order([item], delivery, sId="", dry_run=True)
            return redirect(url_for(return_to))

        session_data = login()
        result = place_order([item], delivery, sId=session_data["sId"], dry_run=False)
        order_no = (result or {}).get("order", {}).get("orderNo", "?")
        flash(f"발주 완료 — 주문번호 {order_no}", "success")
    except Exception as e:
        flash(f"발주 실패: {e}", "error")
    return redirect(url_for(return_to))


if __name__ == "__main__":
    # PORT/HOST가 설정되면(Render 등 외부 배포) 그걸 쓰고, 아니면 로컬 전용 기본값.
    port = int(os.environ.get("PORT", 5050))
    # Render 등은 PORT를 지정해서 실행하므로 그때만 0.0.0.0으로 바인딩 (로컬 실행 시엔 127.0.0.1 유지)
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    print(f"\nFriday — http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
