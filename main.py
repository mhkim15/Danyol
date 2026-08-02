#!/usr/bin/env python3
"""
비브레이브 이커머스 자동화 CLI

사용법:
  # 소싱 후보 추가 (마진 가격 선택 입력)
  python3 main.py sourcing add --keyword 실리콘주걱 --search 3200 --products 450 --category 주방용품
  python3 main.py sourcing add --keyword 실리콘주걱 --search 3200 --products 450 --category 주방용품 --sale-price 12000 --cost-price 6500

  # CSV 일괄 입력 (아이템스카우트 데이터)
  python3 main.py sourcing import --file data/candidates.csv

  # 목록 / 기준 미달 경고
  python3 main.py sourcing list
  python3 main.py sourcing check

  # 트렌드 조회 (네이버 API 키 필요)
  python3 main.py sourcing trend --keyword 실리콘주걱
  python3 main.py sourcing competition --keyword 실리콘주걱

  # 마진 계산
  python3 main.py margin --price 15000 --cost 8000

  # 주간 체크리스트
  python3 main.py report
"""
import argparse
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SOURCING_LOG = DATA_DIR / "sourcing_log.json"
SELECTED_PRODUCTS = DATA_DIR / "selected_products.json"
PRODUCTS_DB = DATA_DIR / "products.json"
CSV_TEMPLATE = DATA_DIR / "candidates.csv"


def _load_env() -> None:
    """루트 디렉토리 .env 파일이 있으면 환경변수로 로드."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        # python-dotenv 없을 때 직접 파싱
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def cmd_sourcing(args: argparse.Namespace) -> None:
    from bebrave.sourcing import analyze, filter_candidates, load_from_json, save_to_json, import_from_csv, check_margin

    if args.sourcing_cmd == "add":
        candidates = load_from_json(SOURCING_LOG)
        candidate, warnings = analyze(
            keyword=args.keyword,
            monthly_search=args.search,
            product_count=args.products,
            category=args.category,
            is_seasonal=args.seasonal,
            notes=args.notes,
            est_sale_price=getattr(args, "sale_price", 0) or 0,
            est_cost_price=getattr(args, "cost_price", 0) or 0,
        )
        candidates.append(candidate)
        save_to_json(candidates, SOURCING_LOG)

        status = "통과" if not warnings else "경고"
        print(f"\n[{status}] '{candidate.keyword}' 추가 완료 | 점수: {candidate.score}점")
        print(f"  골든레이시오: {candidate.golden_ratio} | 검색수: {candidate.monthly_search:,} | 상품수: {candidate.product_count:,}")
        if candidate.est_sale_price and candidate.est_cost_price:
            margin = check_margin(candidate)
            if margin:
                print(f"  {margin.summary()}")
        if warnings:
            for w in warnings:
                print(f"  ! {w}")
        print()

    elif args.sourcing_cmd == "import":
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[오류] 파일을 찾을 수 없습니다: {file_path}")
            print(f"  CSV 양식 예시: {CSV_TEMPLATE}")
            sys.exit(1)

        print(f"\nCSV 일괄 분석: {file_path.name}")
        print("-" * 60)
        results = import_from_csv(file_path)
        if not results:
            print("분석할 항목이 없습니다.")
            return

        candidates = load_from_json(SOURCING_LOG)
        existing_keywords = {c.keyword for c in candidates}
        added, skipped = 0, 0

        for candidate, warnings in sorted(results, key=lambda r: r[0].score, reverse=True):
            if candidate.keyword in existing_keywords:
                print(f"  [건너뜀] '{candidate.keyword}' — 이미 존재")
                skipped += 1
                continue
            status = "통과" if not warnings else "경고"
            print(f"  [{status}] {candidate.keyword:<20} 점수: {candidate.score:>3}점 | 레이시오: {candidate.golden_ratio}")
            if warnings:
                for w in warnings:
                    print(f"         ! {w}")
            candidates.append(candidate)
            existing_keywords.add(candidate.keyword)
            added += 1

        save_to_json(candidates, SOURCING_LOG)
        print(f"\n완료: {added}개 추가 | {skipped}개 건너뜀 | 전체 후보: {len(candidates)}개\n")

    elif args.sourcing_cmd == "list":
        candidates = load_from_json(SOURCING_LOG)
        if not candidates:
            print("저장된 소싱 후보가 없습니다.")
            return
        sorted_list = sorted(candidates, key=lambda c: c.score, reverse=True)
        print(f"\n{'#':<3} {'키워드':<20} {'점수':>4} {'골든레이시오':>12} {'검색수':>8} {'상품수':>7} {'카테고리':<12} 등록일")
        print("-" * 88)
        for i, c in enumerate(sorted_list, 1):
            flag = "" if c.golden_ratio >= 2.0 else " !"
            print(
                f"{i:<3} {c.keyword:<20} {c.score:>4}점 "
                f"{c.golden_ratio:>12}{flag}  {c.monthly_search:>8,} {c.product_count:>7,}  "
                f"{c.category:<12} {c.added_date}"
            )
        print()

    elif args.sourcing_cmd == "check":
        candidates = load_from_json(SOURCING_LOG)
        passed = filter_candidates(candidates)
        failed = [c for c in candidates if c not in passed]

        print(f"\n소싱 현황: 전체 {len(candidates)}개 | 기준통과 {len(passed)}개 | 기준미달 {len(failed)}개")

        if passed:
            print("\n[기준 통과 후보 — 점수 순]")
            for c in sorted(passed, key=lambda c: c.score, reverse=True):
                print(f"  {c.score:>3}점 | {c.keyword} (레이시오: {c.golden_ratio}, 검색: {c.monthly_search:,})")

        if failed:
            print("\n[기준 미달 — 교체 검토]")
            for c in failed:
                print(f"  - {c.keyword} (레이시오: {c.golden_ratio}, 검색: {c.monthly_search:,}, 상품수: {c.product_count:,})")
        print()

    elif args.sourcing_cmd == "trend":
        _load_env()
        from bebrave.sourcing.trend import fetch_trend
        keyword = args.keyword
        print(f"\n'{keyword}' 트렌드 조회 중...")
        try:
            result = fetch_trend(keyword)
            print(f"\n{result.summary()}\n")
        except (ValueError, NotImplementedError) as e:
            print(f"[오류] {e}\n")
            sys.exit(1)

    elif args.sourcing_cmd == "competition":
        _load_env()
        from bebrave.sourcing.competition import fetch_competition
        from bebrave.sourcing.keyword_tool import fetch_related_keywords
        keyword = args.keyword
        print(f"\n'{keyword}' 경쟁 품질 조회 중...")
        try:
            # 쇼핑검색 API 폐지(2026-07-31)로 등록상품수 직접 조회 불가 —
            # 검색광고 API 경쟁지수(comp_idx)로 근사한다.
            related = fetch_related_keywords(keyword, limit=50)
            comp_idx = next((k.comp_idx for k in related if k.keyword == keyword), "")
            result = fetch_competition(keyword, comp_idx=comp_idx)
            print(f"\n{result.summary()}\n")
        except (ValueError, NotImplementedError) as e:
            print(f"[오류] {e}\n")
            sys.exit(1)

    elif args.sourcing_cmd == "template":
        _write_csv_template()

    elif args.sourcing_cmd == "search":
        _load_env()
        from bebrave.sourcing.product_search import search, print_search_report

        keyword = args.keyword
        print(f"\n'{keyword}' 상품 검색 중...")
        try:
            result = search(
                keyword=keyword,
                naver_limit=args.naver_limit,
                supply_limit=args.supply_limit,
                with_domemae=not args.no_domemae,
            )
            print_search_report(result)
        except (ValueError, NotImplementedError) as e:
            print(f"[오류] {e}\n")
            sys.exit(1)

    elif args.sourcing_cmd == "variants":
        _load_env()
        from bebrave.sourcing.product_search import search, print_variants_report

        keyword = args.keyword
        print(f"\n'{keyword}' 변형 카탈로그 탐색 중...")
        try:
            result = search(keyword=keyword, naver_limit=10, supply_limit=30, with_domemae=True)
            print_variants_report(result, top_n=args.count)
        except (ValueError, NotImplementedError) as e:
            print(f"[오류] {e}\n")
            sys.exit(1)

    elif args.sourcing_cmd == "discover":
        _load_env()
        from bebrave.sourcing.discover import (
            discover, to_product_candidates, print_report,
            scan_categories, print_category_ranking,
        )
        from bebrave.sourcing import save_to_json, load_from_json

        category = args.category

        # ── 전 카테고리 비교 스캔 모드 ────────────────────────────
        if category == "all":
            print("\n[전 카테고리 비교 스캔] 경쟁력 있는 카테고리 발굴")
            print(f"{'─'*50}")
            try:
                scores = scan_categories(
                    limit=args.limit,
                    with_domemae=not args.no_domemae,
                    depth=args.depth,
                    max_supply=args.max_supply,
                )
            except (ValueError, NotImplementedError) as e:
                print(f"[오류] {e}\n")
                sys.exit(1)
            print_category_ranking(scores)
            print("  [비교 스캔] 저장 생략 — 단일 카테고리 정밀 탐색 후 저장하세요\n")
            return

        # 키워드 파일 or 자동 탐색
        keywords = None
        if args.keywords:
            kw_path = Path(args.keywords)
            if not kw_path.exists():
                print(f"[오류] 키워드 파일을 찾을 수 없습니다: {kw_path}")
                sys.exit(1)
            keywords = [l.strip() for l in kw_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            print(f"\n키워드 파일: {kw_path.name} ({len(keywords)}개)")
        else:
            print(f"\n[{category}] 롱테일 재귀 발굴 시작")

        print(f"{'─'*50}")

        try:
            results = discover(
                category=category,
                keywords=keywords,
                limit=args.limit,
                with_domemae=not args.no_domemae,
                depth=args.depth,
                max_supply=args.max_supply,
            )
        except (ValueError, NotImplementedError) as e:
            print(f"[오류] {e}\n")
            sys.exit(1)

        print_report(results, category)

        if not args.dry_run and results:
            recommended = [
                r for r in results
                if r.recommendation in ("진입 권장", "진입 가능", "리메이크 권장", "리메이크 검토")
            ]
            if recommended:
                candidates = load_from_json(SOURCING_LOG)
                existing = {c.keyword for c in candidates}
                new_candidates = to_product_candidates(recommended)
                added = 0
                for c in new_candidates:
                    if c.keyword not in existing:
                        candidates.append(c)
                        existing.add(c.keyword)
                        added += 1
                save_to_json(candidates, SOURCING_LOG)
                print(f"  sourcing_log.json 저장: {added}개 추가 (트랙A 진입권장/가능 + 트랙B 리메이크권장/검토 기준)\n")
            else:
                print("  저장할 후보 없음\n")
        elif args.dry_run:
            print("  [dry-run] 저장 생략\n")

        # ── 선택 인터페이스 (--select) ──────────────────────────────────────
        if getattr(args, "select", False) and results:
            _run_select_interface(results)


def _run_select_interface(results) -> None:
    """추천 결과에서 등록할 상품을 번호로 선택 → selected_products.json 저장."""
    import json

    candidates = [r for r in results if r.recommendation in ("진입 권장", "진입 가능")]
    if not candidates:
        print("\n선택 가능한 추천 상품이 없습니다 (진입 권장/가능 0개).\n")
        return

    print(f"\n{'─'*55}")
    print("  추천 상품 — 등록할 번호를 선택하세요")
    print(f"{'─'*55}")
    for i, r in enumerate(candidates, 1):
        margin_str = f"마진 {r.margin_rate:.0%}" if r.margin_rate else "마진 미조회"
        price_str  = f"도매가 {r.supply_price:,}원" if r.supply_price else "도매가 미조회"
        rec_mark   = "★" if r.recommendation == "진입 권장" else " "
        print(f"  {rec_mark}{i:2}. {r.keyword:<18} {margin_str:<10} | {price_str:<16} | 레이시오 {r.golden_ratio:.1f}")
    print(f"{'─'*55}")
    print("  ★ = 진입 권장 | 나머지 = 진입 가능")
    print()

    raw = input("번호 입력 (쉼표 구분, 전체 선택: all, 건너뛰기: Enter): ").strip()
    if not raw:
        print("  선택 없이 종료합니다.\n")
        return

    selected = []
    if raw.lower() == "all":
        selected = candidates
    else:
        for token in raw.split(","):
            token = token.strip()
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(candidates):
                    selected.append(candidates[idx])
                else:
                    print(f"  [경고] 번호 {token}은 범위를 벗어납니다. 건너뜁니다.")

    if not selected:
        print("  유효한 선택이 없습니다.\n")
        return

    # selected_products.json 저장
    SELECTED_PRODUCTS.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if SELECTED_PRODUCTS.exists():
        with open(SELECTED_PRODUCTS, encoding="utf-8") as f:
            existing = json.load(f)
    existing_kws = {p["keyword"] for p in existing}

    added = 0
    for r in selected:
        if r.keyword not in existing_kws:
            existing.append({
                "keyword":      r.keyword,
                "category":     r.category,
                "supply_price": r.supply_price,
                "supply_name":  r.supply_name,
                "naver_price":  int(r.avg_naver_price) if r.avg_naver_price else 0,
                "margin_rate":  round(r.margin_rate, 4) if r.margin_rate else 0,
                "golden_ratio": r.golden_ratio,
                "recommendation": r.recommendation,
            })
            existing_kws.add(r.keyword)
            added += 1

    with open(SELECTED_PRODUCTS, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"\n  {added}개 상품 선택 완료 → data/selected_products.json 저장\n")
    print("  다음 단계: python3 main.py register --from-selected\n")


def _write_csv_template() -> None:
    CSV_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    if CSV_TEMPLATE.exists():
        print(f"이미 존재합니다: {CSV_TEMPLATE}")
        return
    content = (
        "keyword,monthly_search,product_count,category,is_seasonal,notes,est_sale_price,est_cost_price\n"
        "실리콘주걱,3200,450,주방용품,false,,12000,6500\n"
        "다용도선반,5500,800,정리수납,false,,,\n"
    )
    CSV_TEMPLATE.write_text(content, encoding="utf-8-sig")
    print(f"CSV 템플릿 생성: {CSV_TEMPLATE}")
    print("아이템스카우트 데이터를 이 형식으로 채운 후 `sourcing import --file data/candidates.csv`를 실행하세요.")


def cmd_register(args: argparse.Namespace) -> None:
    _load_env()
    from bebrave.smartstore.pipeline import run
    from pathlib import Path

    results = run(
        keyword=args.keyword or "",
        supply_id=args.supply_id or "",
        from_sourcing=args.from_sourcing,
        sourcing_log=Path(args.sourcing_log) if args.sourcing_log else None,
        dry_run=args.dry_run,
        status="SUSPENSION" if args.suspend else "ON",
        output_path=Path(args.output) if args.output else None,
        force=args.force,
    )

    if results:
        print(f"\n완료: {len(results)}개 상품 {'dry-run 미리보기' if args.dry_run else '등록 완료'}")
        for p in results:
            naver_id = f"  → 스마트스토어 ID: {p.naver_product_id}" if p.naver_product_id else ""
            print(f"  - {p.name} ({p.sale_price:,}원, 마진 {p.margin_rate:.1%}){naver_id}")
    else:
        print("\n등록된 상품 없음")


def cmd_orders(args: argparse.Namespace) -> None:
    _load_env()
    from bebrave.smartstore.auth import get_access_token
    from bebrave.smartstore.orders import fetch_new_orders, dispatch_order, DELIVERY_COMPANY_CODES

    token = get_access_token()

    if args.orders_cmd == "check":
        orders = fetch_new_orders(token, hours=args.hours)
        if not orders:
            print(f"\n최근 {args.hours}시간 내 신규 주문 없음")
            return
        print(f"\n신규 주문 {len(orders)}건:\n")
        for o in orders:
            print(o.summary())
            print()

    elif args.orders_cmd == "dispatch":
        print(f"\n[확인] 주문 {args.product_order_id} → 송장번호 {args.tracking_number} ({args.company}) 로 발송처리합니다.")
        confirm = input("진행할까요? (y/n): ").strip().lower()
        if confirm != "y":
            print("취소됨")
            return
        dispatch_order(args.product_order_id, args.tracking_number, args.company, token)
        print("발송처리 완료 — 스마트스토어 주문 상태가 배송중으로 변경됩니다.")

    else:
        print("사용법: orders check | orders dispatch --product-order-id ... --tracking-number ... --company ...")
        print(f"택배사 코드: {list(DELIVERY_COMPANY_CODES.keys())}")


TRACKED_PRODUCTS = DATA_DIR / "tracked_products.json"


def cmd_tracker(args: argparse.Namespace) -> None:
    _load_env()
    from bebrave.tracker.products import ProductTracker

    tracker = ProductTracker(TRACKED_PRODUCTS)

    if args.tracker_cmd == "add":
        from datetime import date
        tracker.add_or_update(args.id, args.name, args.registered_date or date.today().isoformat())
        tracker.save()
        print(f"추적 등록: {args.name} ({args.id})")

    elif args.tracker_cmd == "check":
        stale = tracker.stale_products()
        risky = tracker.auto_delete_risk()
        print(f"\n추적 중 {len(tracker.products)}개 상품")
        if risky:
            print(f"\n[자동삭제 위험 — {13}개월 이상 미판매] {len(risky)}건")
            for p in risky:
                print(f"  - {p.name} ({p.product_id}) — {p.months_since_sold()}개월째 미판매")
        if stale:
            print(f"\n[교체 검토 — {3}개월 이상 미판매] {len(stale)}건")
            for p in stale:
                if p not in risky:
                    print(f"  - {p.name} ({p.product_id}) — {p.months_since_sold()}개월째 미판매")
        if not stale and not risky:
            print("모두 정상 (3개월 이내 판매 이력 있음)")

    elif args.tracker_cmd == "sync":
        from bebrave.smartstore.auth import get_access_token
        from bebrave.smartstore.orders import fetch_new_orders
        token = get_access_token()
        orders = fetch_new_orders(token, hours=args.hours)
        updated = tracker.sync_from_orders(orders)
        tracker.save()
        print(f"주문 {len(orders)}건 조회 → {updated}개 상품 판매일 갱신")

    else:
        print("사용법: tracker add --id --name [--registered-date] | tracker check | tracker sync [--hours]")


def cmd_notify(args: argparse.Namespace) -> None:
    _load_env()
    from bebrave.notify.kakao import build_authorize_url, exchange_code_for_tokens, send_to_me

    if args.notify_cmd == "setup":
        key = os.environ.get("KAKAO_REST_API_KEY", "")
        if not key:
            print(".env에 KAKAO_REST_API_KEY를 먼저 설정하세요 (https://developers.kakao.com 앱의 REST API 키)")
            return
        redirect = args.redirect_uri
        print("\n1) 아래 URL을 브라우저에서 열어 카카오 로그인 + '카카오톡 메시지 전송' 동의:")
        print(f"   {build_authorize_url(key, redirect)}")
        print(f"\n2) 리다이렉트된 URL({redirect}?code=...)에서 code 값을 복사해 아래처럼 실행:")
        print(f"   python3 main.py notify setup --redirect-uri {redirect} --code <복사한_code>")

        if args.code:
            tokens = exchange_code_for_tokens(args.code, key, redirect)
            print("\n발급 완료! 아래 값을 .env에 저장하세요:")
            print(f"  KAKAO_REFRESH_TOKEN={tokens['refresh_token']}")

    elif args.notify_cmd == "test":
        send_to_me("비브레이브 테스트 알림입니다 — 이 메시지가 보이면 연동 성공!", button_title="확인")
        print("발송 완료 — 카카오톡 '나와의 채팅'을 확인하세요.")

    else:
        print("사용법: notify setup [--redirect-uri] [--code] | notify test")


def cmd_purchase(args: argparse.Namespace) -> None:
    _load_env()
    from bebrave.sourcing.domemae_order import login, place_order, OrderItem, DeliveryInfo

    if args.purchase_cmd == "place":
        from bebrave.sourcing.domemae_order import OrderOption
        delivery = DeliveryInfo(
            name=args.receiver_name, email=args.receiver_email,
            zipcode=args.zipcode, address1=args.address1, address2=args.address2 or "",
            phone=args.phone, shop_name=args.shop_name,
        )
        item = OrderItem(goods_no=args.goods_no, options=[OrderOption(quantity=args.qty)])

        if not args.live:
            print("[dry-run 모드] 실제 발주하려면 --live 옵션을 추가하세요.\n")
            place_order([item], delivery, sId="", dry_run=True)
            return

        print(f"\n⚠️  실제 발주 확인 ⚠️")
        print(f"  상품번호: {args.goods_no}  수량: {args.qty}")
        print(f"  수령인: {args.receiver_name} ({args.phone})")
        print(f"  주소: {args.address1} {args.address2}")
        print(f"  이 발주는 실제 이머니가 차감되는 진짜 결제입니다.")
        confirm = input('진행하려면 "발주"라고 입력하세요: ').strip()
        if confirm != "발주":
            print("취소됨")
            return

        session = login()
        result = place_order([item], delivery, sId=session["sId"], dry_run=False)
        print(f"\n발주 완료: 주문번호 {result.get('order', {}).get('orderNo', '?')}")

    else:
        print("사용법: purchase place --goods-no --qty --receiver-name --phone --zipcode --address1 [--live]")


def cmd_margin(args: argparse.Namespace) -> None:
    from bebrave.margin import calculate
    result = calculate(
        sale_price=args.price,
        cost_price=args.cost,
        sales_fee_rate=args.fee_rate,
        free_shipping=args.free_shipping,
    )
    print(f"\n{result.summary()}\n")


def cmd_report(args: argparse.Namespace) -> None:
    from bebrave.report import weekly_summary
    print(weekly_summary())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bebrave",
        description="비브레이브 스마트스토어 자동화 도구",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── sourcing ──────────────────────────────────────────
    sourcing_parser = subparsers.add_parser("sourcing", help="상품 소싱 분석")
    sourcing_sub = sourcing_parser.add_subparsers(dest="sourcing_cmd")

    add_p = sourcing_sub.add_parser("add", help="소싱 후보 1개 추가")
    add_p.add_argument("--keyword", required=True)
    add_p.add_argument("--search", type=int, required=True, help="월간 검색수")
    add_p.add_argument("--products", type=int, required=True, help="등록 상품수")
    add_p.add_argument("--category", required=True, help="카테고리 (주방용품/생활용품 등)")
    add_p.add_argument("--seasonal", action="store_true")
    add_p.add_argument("--notes", default="")
    add_p.add_argument("--sale-price", type=int, default=0, help="예상 판매가 (마진 계산용, 선택)")
    add_p.add_argument("--cost-price", type=int, default=0, help="예상 도매가 (마진 계산용, 선택)")

    import_p = sourcing_sub.add_parser("import", help="CSV 파일 일괄 분석 후 추가")
    import_p.add_argument("--file", required=True, help="CSV 파일 경로")

    sourcing_sub.add_parser("list", help="후보 목록 출력 (점수 순)")
    sourcing_sub.add_parser("check", help="기준 통과/미달 현황")
    sourcing_sub.add_parser("template", help="CSV 입력 템플릿 생성")

    trend_p = sourcing_sub.add_parser("trend", help="네이버 데이터랩 트렌드 조회 (API 키 필요)")
    trend_p.add_argument("--keyword", required=True)

    comp_p = sourcing_sub.add_parser("competition", help="네이버 쇼핑 경쟁 품질 조회 (API 키 필요)")
    comp_p.add_argument("--keyword", required=True)

    search_p = sourcing_sub.add_parser(
        "search",
        help="키워드 기반 상품 검색 (네이버 쇼핑 상위 상품 + 도매매 도매가)",
    )
    search_p.add_argument("--keyword", required=True, help="검색 키워드")
    search_p.add_argument("--naver-limit", type=int, default=10, help="네이버 쇼핑 조회 수 (기본: 10)")
    search_p.add_argument("--supply-limit", type=int, default=10, help="도매매 조회 수 (기본: 10)")
    search_p.add_argument("--no-domemae", action="store_true", help="도매매 조회 생략")

    variants_p = sourcing_sub.add_parser(
        "variants",
        help="검증된 키워드의 도매매 변형 상품(디자인·색상 다른 것) 여러 개 탐색 — 변형 카탈로그",
    )
    variants_p.add_argument("--keyword", required=True, help="이미 검증된 키워드")
    variants_p.add_argument("--count", type=int, default=5, help="탐색할 변형 후보 수 (기본: 5)")

    disc_p = sourcing_sub.add_parser(
        "discover",
        help="전체 자동 소싱 탐색 (데이터랩 + 쇼핑 + 도매매 API 키 필요)",
    )
    disc_p.add_argument(
        "--category", default="주방용품",
        help="소싱 카테고리 (주방용품/욕실용품/생활용품/정리수납). 'all'이면 전 카테고리 비교 스캔"
    )
    disc_p.add_argument(
        "--keywords", default="",
        help="키워드 파일 경로 (한 줄에 키워드 하나). 미지정 시 재귀 자동 탐색"
    )
    disc_p.add_argument("--limit", type=int, default=20, help="최종 결과 키워드 수 (기본: 20)")
    disc_p.add_argument("--depth", type=int, default=2, help="키워드 재귀 확장 단계 수 (기본: 2)")
    disc_p.add_argument("--max-supply", type=int, default=30_000, help="등록 상품수 하드 상한 (기본: 30000)")
    disc_p.add_argument("--no-domemae", action="store_true", help="도매매 도매가 조회 생략")
    disc_p.add_argument("--dry-run", action="store_true", help="조회만 하고 저장 안 함")
    disc_p.add_argument("--select", action="store_true", help="탐색 후 등록할 상품 번호 선택 인터페이스 실행")

    # ── orders ────────────────────────────────────────────
    orders_parser = subparsers.add_parser("orders", help="스마트스토어 주문 조회 및 발송처리 (커머스 API 필요)")
    orders_sub = orders_parser.add_subparsers(dest="orders_cmd")

    check_p = orders_sub.add_parser("check", help="최근 신규(결제완료) 주문 조회")
    check_p.add_argument("--hours", type=int, default=24, help="조회 범위 (시간, 기본 24)")

    dispatch_p = orders_sub.add_parser("dispatch", help="송장번호 입력 → 발송처리")
    dispatch_p.add_argument("--product-order-id", required=True)
    dispatch_p.add_argument("--tracking-number", required=True)
    dispatch_p.add_argument("--company", required=True, help="예: CJ대한통운, 롯데택배, 우체국택배, 한진택배, 로젠택배")

    # ── tracker ───────────────────────────────────────────
    tracker_parser = subparsers.add_parser("tracker", help="등록 상품 판매현황 추적 (미판매·자동삭제 위험 감지)")
    tracker_sub = tracker_parser.add_subparsers(dest="tracker_cmd")

    tadd_p = tracker_sub.add_parser("add", help="추적 대상 상품 등록")
    tadd_p.add_argument("--id", required=True, help="스마트스토어 상품 ID")
    tadd_p.add_argument("--name", required=True)
    tadd_p.add_argument("--registered-date", default="", help="YYYY-MM-DD (기본: 오늘)")

    tracker_sub.add_parser("check", help="미판매·자동삭제 위험 상품 조회")

    tsync_p = tracker_sub.add_parser("sync", help="주문 데이터로 판매일 자동 갱신 (커머스 API 필요)")
    tsync_p.add_argument("--hours", type=int, default=24 * 30, help="조회 범위 (시간, 기본 30일)")

    # ── notify ────────────────────────────────────────────
    notify_parser = subparsers.add_parser("notify", help="카카오톡 '나에게 보내기' 알림 설정/테스트")
    notify_sub = notify_parser.add_subparsers(dest="notify_cmd")

    nsetup_p = notify_sub.add_parser("setup", help="최초 1회 카카오 로그인 인증 URL 발급 및 토큰 교환")
    nsetup_p.add_argument("--redirect-uri", default="https://localhost", help="카카오 앱에 등록한 Redirect URI")
    nsetup_p.add_argument("--code", default="", help="인증 후 리다이렉트 URL의 code 파라미터")

    notify_sub.add_parser("test", help="테스트 메시지 발송")

    # ── purchase ──────────────────────────────────────────
    purchase_parser = subparsers.add_parser("purchase", help="도매매 자동 발주 (실제 결제 — 이머니 필요)")
    purchase_sub = purchase_parser.add_subparsers(dest="purchase_cmd")

    place_p = purchase_sub.add_parser("place", help="도매매 상품 발주")
    place_p.add_argument("--goods-no", required=True, help="도매매 상품번호")
    place_p.add_argument("--qty", type=int, default=1)
    place_p.add_argument("--receiver-name", required=True)
    place_p.add_argument("--receiver-email", default="")
    place_p.add_argument("--phone", required=True)
    place_p.add_argument("--zipcode", required=True)
    place_p.add_argument("--address1", required=True)
    place_p.add_argument("--address2", default="")
    place_p.add_argument("--shop-name", required=True, help="쇼핑몰명/상호명 (도매꾹 상표 노출 방지용, 필수)")
    place_p.add_argument("--live", action="store_true", help="실제로 발주 실행 (기본은 dry-run)")

    # ── margin ────────────────────────────────────────────
    margin_parser = subparsers.add_parser("margin", help="마진 계산 (2026 수수료 기준)")
    margin_parser.add_argument("--price", type=int, required=True, help="판매가 (원)")
    margin_parser.add_argument("--cost", type=int, required=True, help="도매가 (원)")
    margin_parser.add_argument("--fee-rate", type=float, default=0.04)
    margin_parser.add_argument("--free-shipping", action="store_true")

    # ── register ──────────────────────────────────────────
    reg_p = subparsers.add_parser(
        "register",
        help="도매매 상품을 스마트스토어에 자동 등록 (커머스 API 필요)",
    )
    reg_p.add_argument("--keyword", default="", help="키워드로 도매매 검색 후 최저가 상품 등록")
    reg_p.add_argument("--supply-id", default="", help="도매매 상품번호 직접 지정")
    reg_p.add_argument("--from-sourcing", action="store_true", help="sourcing_log에서 진입권장 상품 일괄 등록")
    reg_p.add_argument("--sourcing-log", default="", help="sourcing_log.json 경로 (기본: data/sourcing_log.json)")
    reg_p.add_argument("--dry-run", action="store_true", help="등록 직전까지만 실행 (실제 API 호출 생략)")
    reg_p.add_argument("--suspend", action="store_true", default=True, help="판매중지 상태로 등록 (기본, 수동 확인 후 활성화)")
    reg_p.add_argument("--on-sale", action="store_false", dest="suspend", help="즉시 판매중 상태로 등록")
    reg_p.add_argument("--output", default="", help="결과 저장 경로 (기본: data/registered_products.json)")
    reg_p.add_argument("--force", action="store_true", help="부실 리스팅 경고(사진 1장/설명 부족/저해상도)가 있어도 등록 강행")

    # ── report ────────────────────────────────────────────
    subparsers.add_parser("report", help="주간 체크리스트")

    args = parser.parse_args()

    if args.command == "sourcing":
        if not args.sourcing_cmd:
            sourcing_parser.print_help()
        else:
            cmd_sourcing(args)
    elif args.command == "register":
        cmd_register(args)
    elif args.command == "orders":
        if not args.orders_cmd:
            orders_parser.print_help()
        else:
            cmd_orders(args)
    elif args.command == "tracker":
        if not args.tracker_cmd:
            tracker_parser.print_help()
        else:
            cmd_tracker(args)
    elif args.command == "notify":
        if not args.notify_cmd:
            notify_parser.print_help()
        else:
            cmd_notify(args)
    elif args.command == "purchase":
        if not args.purchase_cmd:
            purchase_parser.print_help()
        else:
            cmd_purchase(args)
    elif args.command == "margin":
        cmd_margin(args)
    elif args.command == "report":
        cmd_report(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
