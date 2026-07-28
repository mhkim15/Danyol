"""
Layer 3 — 네이버 쇼핑 검색 API 연동.
키워드의 등록 상품수(total)로 경쟁 강도를 판단한다.

API: GET https://openapi.naver.com/v1/search/shop.json
- total: 해당 키워드 등록 상품 총 수
- reviewCount: API 미지원 → 등록 상품수로 경쟁 강도 대체 판단

경쟁 강도 기준:
  low  : 1,000개 이하  → 진입 쉬움
  mid  : 1,000~5,000개 → 진입 가능 (SEO 최적화 필요)
  high : 5,000개 초과  → 진입 어려움
"""
import os
import re
from dataclasses import dataclass, field
from typing import List

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


@dataclass
class CompetitionResult:
    keyword: str
    product_count: int       # 네이버 쇼핑 등록 상품 총 수 (total)
    barrier: str             # 'low' | 'mid' | 'high'
    top_prices: List[int] = field(default_factory=list)  # 상위 상품 가격 (판매가 참고용)
    avg_price: float = 0.0   # 상위 상품 평균 판매가 (참고용 — 리뷰·신뢰 쌓인 기존셀러 가격대)
    entry_price: float = 0.0  # 신규셀러 예상 진입가 (마진 계산 기준가) — 상위가격 하위 1/3 지점
    top_titles: List[str] = field(default_factory=list)  # 상위 상품명 (도매매 타입매칭용)

    def summary(self) -> str:
        barrier_str = {
            "low": "약함 (1,000개 이하, 진입 쉬움)",
            "mid": "보통 (1,000~5,000개, SEO 최적화 필요)",
            "high": "강함 (5,000개 초과, 진입 어려움)",
        }.get(self.barrier, "?")
        price_str = f" | 상위 평균가: {self.avg_price:,.0f}원" if self.avg_price else ""
        entry_str = f" | 신규셀러 진입가(추정): {self.entry_price:,.0f}원" if self.entry_price else ""
        return (
            f"경쟁 강도: {barrier_str}\n"
            f"  등록 상품수: {self.product_count:,}개{price_str}{entry_str}"
        )


def fetch_competition(
    keyword: str,
    client_id: str = "",
    client_secret: str = "",
) -> CompetitionResult:
    """
    네이버 쇼핑 검색 API로 등록 상품수 + 상위 상품 가격 조회.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("requests 패키지가 필요합니다. pip3 install requests")

    # 검색 API 전용 키 우선 사용, 없으면 공통 키 fallback
    cid = client_id or os.environ.get("NAVER_SEARCH_CLIENT_ID") or os.environ.get("NAVER_CLIENT_ID", "")
    csecret = client_secret or os.environ.get("NAVER_SEARCH_CLIENT_SECRET") or os.environ.get("NAVER_CLIENT_SECRET", "")
    if not cid or not csecret:
        raise ValueError(".env 파일에 NAVER_SEARCH_CLIENT_ID, NAVER_SEARCH_CLIENT_SECRET을 설정하세요.")

    params = {
        "query": keyword,
        "display": 10,
        "sort": "sim",   # 정확도순 (쇼핑 기본 정렬 = 실제 상위 노출 순서)
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

    product_count = int(data.get("total", 0))

    # 상위 상품 가격 수집 (판매가 참고용)
    items = data.get("items", [])
    prices = []
    titles = []
    for item in items:
        lp = item.get("lprice", 0)
        try:
            prices.append(int(lp))
        except (ValueError, TypeError):
            pass
        raw_title = item.get("title", "")
        titles.append(re.sub(r"<[^>]+>", "", raw_title))

    avg_price = sum(prices) / len(prices) if prices else 0.0

    # 신규셀러 진입가 — 리뷰 0인 신규 스토어는 상위평균가가 아니라 하위권
    # 가격대에서 시작해야 노출·전환이 붙는다. 상위가격을 오름차순 정렬해
    # 하위 1/3 지점 값을 "실제 도달 가능한 가격"으로 근사한다.
    entry_price = 0.0
    if prices:
        sorted_prices = sorted(prices)
        idx = max(0, len(sorted_prices) // 3 - 1)
        entry_price = float(sorted_prices[idx])

    if product_count <= 1_000:
        barrier = "low"
    elif product_count <= 5_000:
        barrier = "mid"
    else:
        barrier = "high"

    return CompetitionResult(
        keyword=keyword,
        product_count=product_count,
        barrier=barrier,
        top_prices=prices,
        avg_price=round(avg_price, 0),
        entry_price=round(entry_price, 0),
        top_titles=titles,
    )


def fetch_product_count(keyword: str, client_id: str = "", client_secret: str = "") -> int:
    """등록 상품수만 빠르게 조회 (discover 파이프라인 내부 사용)."""
    result = fetch_competition(keyword, client_id, client_secret)
    return result.product_count
