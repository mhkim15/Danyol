"""
키워드 기반 상품 검색 자동화.

선별된 키워드 → 네이버 쇼핑 상위 실제 상품 + 도매매 도매 상품을 동시 조회해
어떤 상품을 어떤 공급사에서 소싱할지 빠르게 파악한다.

CLI:
  python3 main.py sourcing search --keyword 실리콘주걱
  python3 main.py sourcing search --keyword 실리콘주걱 --naver-limit 10 --supply-limit 10
  python3 main.py sourcing search --keyword 실리콘주걱 --no-domemae
"""
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ── 네이버 쇼핑 상품 ───────────────────────────────────────────────────────────

@dataclass
class NaverProduct:
    title: str                # 상품명 (HTML 태그 제거)
    price: int                # 최저가 (원)
    mall_name: str            # 판매 스토어명
    review_count: int         # 리뷰 수
    category: str             # 카테고리 (대분류)
    brand: str                # 브랜드
    link: str                 # 상품 링크
    image: str = ""           # 썸네일 URL

    def summary(self) -> str:
        return (
            f"  {self.title[:35]:<35} "
            f"{self.price:>8,}원  리뷰:{self.review_count:>5,}  [{self.mall_name}]"
        )


def fetch_naver_products(
    keyword: str,
    limit: int = 10,
    client_id: str = "",
    client_secret: str = "",
) -> List[NaverProduct]:
    """
    네이버 쇼핑 검색 API → 상위 실제 상품 목록 반환.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    cid = client_id or os.environ.get("NAVER_SEARCH_CLIENT_ID") or os.environ.get("NAVER_CLIENT_ID", "")
    csecret = client_secret or os.environ.get("NAVER_SEARCH_CLIENT_SECRET") or os.environ.get("NAVER_CLIENT_SECRET", "")
    if not cid or not csecret:
        raise ValueError(".env 파일에 NAVER_SEARCH_CLIENT_ID, NAVER_SEARCH_CLIENT_SECRET을 설정하세요.")

    params = {
        "query": keyword,
        "display": min(limit, 100),
        "sort": "sim",
    }
    headers = {
        "X-Naver-Client-Id": cid,
        "X-Naver-Client-Secret": csecret,
    }
    resp = requests.get(
        "https://openapi.naver.com/v1/search/shop.json",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    products = []
    for item in data.get("items", []):
        title = _strip_html(item.get("title", ""))
        price = _to_int(item.get("lprice", 0))
        mall = item.get("mallName", "")
        brand = item.get("brand", "")
        cat = item.get("category1", "")
        link = item.get("link", "")
        image = item.get("image", "")
        # 리뷰수: 기본 API에서 제공 안 함 → reviewCount 있으면 사용
        review = _to_int(item.get("reviewCount", item.get("review", 0)))

        if title and price:
            products.append(NaverProduct(
                title=title,
                price=price,
                mall_name=mall,
                review_count=review,
                category=cat,
                brand=brand,
                link=link,
                image=image,
            ))

    return products


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


def _to_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


# ── 통합 검색 결과 ─────────────────────────────────────────────────────────────

@dataclass
class ProductSearchResult:
    keyword: str
    naver_total: int                                      # 네이버 쇼핑 총 등록 상품수
    naver_avg_price: float                                # 상위 상품 평균 판매가 (참고용)
    naver_entry_price: float = 0.0                        # 신규셀러 예상 진입가 (마진 계산 기준가)
    naver_products: List[NaverProduct] = field(default_factory=list)
    supply_products: List = field(default_factory=list)  # DomemaeProduct 리스트
    supply_total: int = 0                                 # 도매매 검색 결과 수
    error: str = ""


def search(
    keyword: str,
    naver_limit: int = 10,
    supply_limit: int = 10,
    with_domemae: bool = True,
) -> ProductSearchResult:
    """
    키워드 기반 통합 상품 검색.

    Args:
        keyword       : 검색 키워드
        naver_limit   : 네이버 쇼핑 조회 상품 수
        supply_limit  : 도매매 조회 상품 수
        with_domemae  : 도매매 조회 여부

    Returns:
        ProductSearchResult
    """
    errors = []

    # 1. 네이버 쇼핑 총 상품수 + 상위 상품 목록
    naver_products: List[NaverProduct] = []
    naver_total = 0
    naver_avg_price = 0.0
    naver_entry_price = 0.0

    try:
        from .competition import fetch_competition
        from .keyword_tool import fetch_related_keywords
        # 쇼핑검색 API 폐지(2026-07-31)로 등록상품수 직접 조회 불가 —
        # 검색광고 API 경쟁지수(comp_idx)로 근사한다.
        related = fetch_related_keywords(keyword, limit=50)
        comp_idx = next((k.comp_idx for k in related if k.keyword == keyword), "")
        comp = fetch_competition(keyword, comp_idx=comp_idx)
        naver_total = comp.product_count
        naver_avg_price = comp.avg_price
        naver_entry_price = comp.entry_price
    except Exception as e:
        errors.append(f"네이버쇼핑 집계 실패: {e}")

    try:
        naver_products = fetch_naver_products(keyword, limit=naver_limit)
    except Exception as e:
        errors.append(f"네이버쇼핑 상품 조회 실패: {e}")

    time.sleep(0.3)

    # 2. 도매매 도매 상품 검색
    supply_products = []
    supply_total = 0

    if with_domemae:
        domemae_key = os.environ.get("DOMEMAE_API_KEY", "")
        if domemae_key:
            try:
                from .domemae import search_products
                result = search_products(keyword, limit=supply_limit)
                supply_products = result.products
                supply_total = result.total
            except Exception as e:
                errors.append(f"도매매 조회 실패: {e}")
        else:
            errors.append("DOMEMAE_API_KEY 미설정 (도매매 건너뜀)")

    return ProductSearchResult(
        keyword=keyword,
        naver_total=naver_total,
        naver_avg_price=naver_avg_price,
        naver_entry_price=naver_entry_price,
        naver_products=naver_products,
        supply_products=supply_products,
        supply_total=supply_total,
        error="; ".join(errors),
    )


# ── 리포트 출력 ────────────────────────────────────────────────────────────────

def print_search_report(result: ProductSearchResult) -> None:
    """터미널 검색 결과 리포트."""
    kw = result.keyword

    print(f"\n{'═'*70}")
    print(f"  상품 검색 결과 — [{kw}]")
    print(f"{'═'*70}")

    # ── 네이버 쇼핑 ──────────────────────────────────────────────────────────
    print(f"\n  [네이버 쇼핑]  총 {result.naver_total:,}개 등록  |  상위 평균가: {result.naver_avg_price:,.0f}원  |  신규셀러 진입가(추정): {result.naver_entry_price:,.0f}원")
    print(f"  {'─'*65}")
    if result.naver_products:
        print(f"  {'#':>2}  {'상품명':<36} {'최저가':>9}  {'리뷰':>6}  스토어")
        print(f"  {'─'*65}")
        for i, p in enumerate(result.naver_products, 1):
            print(f"  {i:>2}  {p.title[:35]:<36} {p.price:>8,}원  {p.review_count:>5,}  {p.mall_name}")
    else:
        print("  조회 결과 없음")

    # ── 도매매 ────────────────────────────────────────────────────────────────
    if result.supply_products or "DOMEMAE_API_KEY 미설정" not in result.error:
        print(f"\n  [도매매 도매가]  검색결과 {result.supply_total:,}개")
        print(f"  {'─'*65}")
        if result.supply_products:
            print(f"  {'#':>2}  {'상품명':<36} {'도매가':>9}  {'최소수량':>6}  공급사")
            print(f"  {'─'*65}")
            for i, p in enumerate(result.supply_products, 1):
                print(f"  {i:>2}  {p.name[:35]:<36} {p.supply_price:>8,}원  {p.min_order_qty:>5,}개  {p.supplier}")

            # 마진 계산 (타입 일치 확인된 도매가 vs 신규셀러 진입가)
            if result.naver_entry_price and result.supply_products:
                from .domemae import find_matching_product
                naver_titles = [p.title for p in result.naver_products]
                matched_product, matched = find_matching_product(naver_titles, result.supply_products)
                try:
                    from ..margin.calculator import calculate as calc_margin
                    margin = calc_margin(
                        sale_price=int(result.naver_entry_price),
                        cost_price=matched_product.supply_price,
                        free_shipping=(result.naver_entry_price >= 30_000),
                    )
                    flag = "✓ 목표달성" if (margin.passes_target and margin.passes_abs_floor) else "△ 목표미달"
                    warn = "\n  ⚠ 상품타입 일치 미확인 — 위 상품이 실제로 같은 종류인지 실물 확인 필요" if not matched else ""
                    print(f"\n  [마진 추정]  최저 도매가 {matched_product.supply_price:,}원 x 판매가(신규셀러 진입가) {result.naver_entry_price:,.0f}원{warn}")
                    print(f"    → 마진율: {margin.margin_rate:.1%}  {flag}")
                    print(f"    → {margin.summary()}")
                except Exception:
                    pass
        else:
            print("  조회 결과 없음")

    if result.error:
        for msg in result.error.split("; "):
            if msg:
                print(f"\n  [참고] {msg}")


# ── 변형 카탈로그 (2026-07) ──────────────────────────────────────────────────
# 이미 수요가 검증된 키워드 밑에서 디자인/색상이 다른 도매매 변형 상품을
# 여러 개 찾아 마진까지 계산 — 신규 키워드 발굴 없이 등록 물량을 늘리는 용도.

def print_variants_report(result: ProductSearchResult, top_n: int = 5) -> None:
    """검증된 키워드에 대해 형태가 일치하는 도매매 변형 후보를 가격순으로 여러 개 출력."""
    from .domemae import find_all_matches
    from ..margin.calculator import calculate as calc_margin

    kw = result.keyword
    print(f"\n{'═'*70}")
    print(f"  변형 카탈로그 후보 — [{kw}]")
    print(f"{'═'*70}")
    print(f"  네이버 상위 평균가: {result.naver_avg_price:,.0f}원  |  신규셀러 진입가(추정): {result.naver_entry_price:,.0f}원")

    if not result.supply_products:
        print("\n  도매매 검색 결과 없음")
        return

    naver_titles = [p.title for p in result.naver_products]
    variants = find_all_matches(naver_titles, result.supply_products, top_n=top_n)

    if not variants:
        print("\n  형태 일치하는 변형 후보 없음 (⚠ 전부 매칭 불확실 — 수동 확인 필요)")
        return

    print(f"\n  {'#':>2}  {'상품명':<36} {'도매가':>9}  {'공급사':<16}  {'마진율':>7}  판정")
    print(f"  {'─'*80}")
    for i, p in enumerate(variants, 1):
        margin_str, flag = "─", "─"
        if result.naver_entry_price:
            m = calc_margin(
                sale_price=int(result.naver_entry_price),
                cost_price=p.supply_price,
                free_shipping=(result.naver_entry_price >= 30_000),
            )
            margin_str = f"{m.margin_rate:.1%}"
            flag = "✓통과" if (m.passes_target and m.passes_abs_floor) else "△미달"
        print(f"  {i:>2}  {p.name[:35]:<36} {p.supply_price:>8,}원  {p.supplier:<16}  {margin_str:>7}  {flag}")
    print(f"\n  다음 단계: 마음에 드는 변형 goods_no로 도매매 상세 확인 후 등록\n{'═'*70}\n")

    print(f"\n{'═'*70}\n")
