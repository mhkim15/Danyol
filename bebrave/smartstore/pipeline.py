"""
도매매 → 스마트스토어 자동 등록 파이프라인.

흐름:
  1. 도매매 키워드 검색 or 상품번호 직접 조회
  2. 상품 상세 정보 + 이미지 취득
  3. 마진 계산 → 판매가 결정 (목표 마진율 20%)
  4. AI 상품명 + 상세설명 생성
  5. 커머스 API 토큰 발급
  6. 스마트스토어 상품 등록 (SUSPENSION 상태)
  7. 결과 저장

CLI:
  python3 main.py register --keyword 실리콘주걱
  python3 main.py register --supply-id 12345678
  python3 main.py register --from-sourcing --dry-run
"""
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from ..margin.calculator import calculate as calc_margin
from ..sourcing.domemae import DomemaeProduct, fetch_product_detail, search_products
from ..sourcing.analyzer import load_from_json
from .auth import get_access_token
from .category import get_category_id
from .content import generate_product_content
from .models import StoreProduct
from .register import build_request_body, register_product

_TARGET_MARGIN = float(os.environ.get("TARGET_MARGIN", "0.20"))
_MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "0.15"))


def run(
    keyword: str = "",
    supply_id: str = "",
    from_sourcing: bool = False,
    sourcing_log: Optional[Path] = None,
    dry_run: bool = False,
    status: str = "SUSPENSION",
    output_path: Optional[Path] = None,
    name_override: str = "",
) -> List[StoreProduct]:
    """
    전체 자동 등록 파이프라인.

    Args:
        keyword      : 키워드로 도매매 검색
        supply_id    : 도매매 상품번호 직접 지정 — 지정하면 검색을 건너뛰고 정확히 이 상품만
                       처리하므로, 미리보기에서 확인한 상품과 실제 등록 상품이 어긋나지 않음
                       (키워드 검색은 재고/가격 변동에 따라 다른 최저가 상품이 뽑힐 수 있음)
        from_sourcing: sourcing_log.json에서 "진입 권장" 상품 자동 처리
        sourcing_log : sourcing_log.json 경로
        dry_run      : True면 실제 등록 안 하고 요청 바디만 출력
        status       : 등록 상태 (SUSPENSION / ON)
        output_path  : 등록 결과 저장 경로
        name_override: 지정하면 AI/자동생성 상품명 대신 이 값을 그대로 사용 (미리보기에서
                       사용자가 수정한 이름을 반영할 때 사용)

    Returns:
        등록 완료된 StoreProduct 리스트
    """
    domemae_products: List[DomemaeProduct] = []

    # ── Step 1: 도매매 상품 수집 ───────────────────────────────────────────
    if supply_id:
        print(f"\n[1] 도매매 상품 상세 조회: {supply_id}")
        try:
            p = fetch_product_detail(supply_id)
            domemae_products = [p]
            print(f"  → {p.name} (도매가: {p.supply_price:,}원)")
        except Exception as e:
            print(f"  [오류] 상품 조회 실패: {e}")
            return []

    elif keyword:
        print(f"\n[1] 도매매 키워드 검색: '{keyword}'")
        try:
            result = search_products(keyword, limit=5)
            if not result.products:
                print("  검색 결과 없음")
                return []
            # 최저가 상품 선택
            domemae_products = [result.cheapest or result.products[0]]
            p = domemae_products[0]
            print(f"  → {p.name} (도매가: {p.supply_price:,}원, {result.total}개 결과 중 최저가)")

            # 상세 정보 재조회 (이미지 포함)
            if p.goods_no:
                try:
                    detailed = fetch_product_detail(p.goods_no)
                    domemae_products = [detailed]
                except Exception:
                    pass  # 상세 조회 실패 시 검색 결과 그대로 사용
        except Exception as e:
            print(f"  [오류] 도매매 검색 실패: {e}")
            return []

    elif from_sourcing:
        print("\n[1] 소싱 로그에서 진입 권장 상품 로드")
        log_path = sourcing_log or Path("data/sourcing_log.json")
        candidates = load_from_json(log_path)
        recommended = [c for c in candidates if c.score >= 70]
        if not recommended:
            print("  진입 권장(70점 이상) 상품 없음")
            return []
        print(f"  → {len(recommended)}개 상품 처리 예정")
        # 각 키워드별로 파이프라인 실행
        results = []
        for c in recommended:
            print(f"\n{'─'*50}")
            sub = run(
                keyword=c.keyword,
                dry_run=dry_run,
                status=status,
                output_path=output_path,
            )
            results.extend(sub)
            time.sleep(1.0)
        return results

    else:
        print("[오류] --keyword, --supply-id, --from-sourcing 중 하나를 지정하세요.")
        return []

    registered = []

    for domemae_p in domemae_products:
        kw = keyword or domemae_p.name.split()[0]

        # ── Step 2: 마진 계산 → 판매가 결정 ──────────────────────────────
        print(f"\n[2] 마진 계산 (도매가: {domemae_p.supply_price:,}원)")
        sale_price = _decide_sale_price(domemae_p.supply_price, domemae_p.retail_price)
        margin = calc_margin(
            sale_price=sale_price,
            cost_price=domemae_p.supply_price,
            free_shipping=(sale_price >= 30_000),
        )
        margin_flag = "✓" if margin.passes_target else ("△" if margin.passes_min else "✗")
        print(f"  판매가: {sale_price:,}원  마진율: {margin.margin_rate:.1%} {margin_flag}")

        if not margin.passes_min:
            print(f"  [건너뜀] 최소 마진율({_MIN_MARGIN:.0%}) 미달")
            continue

        # 리스팅 품질 경고 — 사진 1장뿐이거나 설명이 짧으면 최저가만 보고 고른
        # 부실한 리스팅일 가능성이 높음 (2026-07-12: 실전 등록 테스트로 발견된 패턴)
        if len(domemae_p.images) <= 1:
            print(f"  [경고] 이미지가 {len(domemae_p.images)}장뿐 — 원본 리스팅이 부실할 수 있음, 등록 전 육안 확인 권장")
        if len(domemae_p.description) < 200:
            print(f"  [경고] 상세설명이 {len(domemae_p.description)}자로 짧음 — 원본 리스팅이 부실할 수 있음, 등록 전 육안 확인 권장")
        if domemae_p.images:
            from .images import check_min_resolution
            size = check_min_resolution(domemae_p.images[0])
            if size and min(size) < 1000:
                print(f"  [경고] 대표이미지 해상도 {size[0]}x{size[1]}px — 네이버 권장 최소(1000px) 미만, 확대시 흐릿하게 보일 수 있음")

        # ── Step 3: 카테고리 결정 (실제 커머스 API 카테고리 트리 기반 검색) ──
        token = get_access_token()
        cat_id = get_category_id(kw, domemae_p.category, token)
        if not cat_id:
            print(f"\n[3] [건너뜀] 카테고리 자동 매칭 실패 (키워드: '{kw}', 도매매 카테고리: '{domemae_p.category}') — 잘못된 카테고리로 등록되는 걸 방지하기 위해 건너뜀. 수동으로 카테고리 지정 필요")
            continue
        from .category import describe_category
        print(f"\n[3] 카테고리 ID: {cat_id} ({describe_category(cat_id, token)})")

        # ── Step 4: AI 콘텐츠 생성 ────────────────────────────────────────
        print(f"\n[4] 상품 콘텐츠 생성 중...")
        content = generate_product_content(kw, domemae_p, sale_price)
        if name_override:
            content["name"] = name_override
        print(f"  상품명: {content['name']}")

        # ── Step 5: StoreProduct 구성 ─────────────────────────────────────
        store_product = StoreProduct(
            name=content["name"],
            leaf_category_id=cat_id,
            sale_price=sale_price,
            stock_quantity=min(domemae_p.stock, 999),
            detail_content=content["detail_content"],
            representative_image=domemae_p.main_image,
            optional_images=domemae_p.images[1:4],
            supply_price=domemae_p.supply_price,
            margin_rate=margin.margin_rate,
            domemae_goods_no=domemae_p.goods_no,
            supplier=domemae_p.supplier,
            keyword=kw,
            tags=content.get("tags", []),
        )

        if dry_run:
            # ── dry-run: 등록 바디 출력 ────────────────────────────────
            print(f"\n[dry-run] 등록 요청 바디 미리보기:")
            import json as _json
            body = build_request_body(store_product, status=status)
            print(_json.dumps(body, ensure_ascii=False, indent=2)[:1000])
            print(f"\n  {store_product.summary()}")
            registered.append(store_product)
            continue

        # ── Step 6: 커머스 API 토큰 + 이미지 업로드 + 상품 등록 ───────────
        print(f"\n[5] 스마트스토어 상품 등록 중...")
        try:
            token = get_access_token()

            from .images import upload_images
            all_images = [store_product.representative_image] + store_product.optional_images
            all_images = [u for u in all_images if u]
            uploaded = upload_images(all_images, token)
            if not uploaded:
                raise RuntimeError("이미지 업로드 실패 — 등록 가능한 이미지 없음")
            store_product.representative_image = uploaded[0]
            store_product.optional_images = uploaded[1:]
            print(f"  이미지 {len(uploaded)}개 업로드 완료")

            product_id = register_product(store_product, token, status=status)
            store_product.naver_product_id = product_id
            print(f"  등록 완료! 상품 ID: {product_id}  상태: {status}")
        except Exception as e:
            print(f"  [오류] 등록 실패: {e}")
            continue

        registered.append(store_product)

        # ── Step 7: 결과 저장 ─────────────────────────────────────────────
        _save_result(store_product, output_path)

    return registered


def _decide_sale_price(supply_price: int, retail_price: int) -> int:
    """
    판매가 결정 로직.
    목표 마진율 20% 달성 가능한 최소 판매가로 설정.
    소비자가가 있으면 참고하되, 마진율 기준 우선.
    """
    # 마진율 20% 달성 판매가 역산
    # 순이익 = 판매가 - 공급가 - 주문관리수수료(3.63%) - 판매수수료(4%) - CS예비(2.5%) - 배송
    # margin_rate = (판매가 - 공급가 - 판매가*0.1013) / 판매가 >= 0.20
    # 판매가 * (1 - 0.1013 - 0.20) >= 공급가
    # 판매가 >= 공급가 / 0.6987
    target_price = int(supply_price / 0.70) + 1

    # 소비자가가 있고 목표 마진율을 달성하면 소비자가 참고
    if retail_price and retail_price >= target_price:
        # 소비자가보다 5~10% 낮게 시작
        competitor_based = int(retail_price * 0.92)
        if competitor_based >= target_price:
            return competitor_based

    # 100원 단위 올림
    return ((target_price // 100) + 1) * 100


def _save_result(product: StoreProduct, output_path: Optional[Path]) -> None:
    path = output_path or Path("data/registered_products.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing.append(product.to_dict())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
