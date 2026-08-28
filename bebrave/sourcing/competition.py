"""
Layer 3 — 경쟁 강도 판단.

원래는 네이버 쇼핑 검색 API(GET /v1/search/shop.json)의 등록 상품수(total)로
경쟁 강도를 판단했으나, 이 API가 2026-07-31 대체 없이 영구 종료됐다
(개발자센터 공지 #32530 — NAVER API HUB 이관 대상에서도 제외됨).
등록 상품수·상위 판매가·판매처명 모두 이 API 하나에서 나오던 데이터라
전부 조회 불가능해졌다.

대신 네이버 검색광고 API의 경쟁지수(comp_idx: 낮음/중간/높음, keyword_tool.py에서
이미 조회해 옴 — 새 API 호출 불필요)로 경쟁 강도를 근사한다. product_count는
더 이상 실제 값이 아니라 항상 0(미조회)이고, 화면 표시는 comp_idx 라벨을 직접 쓴다.
"""
from dataclasses import dataclass, field
from typing import List

_COMP_IDX_TO_BARRIER = {"낮음": "low", "중간": "mid", "높음": "high"}


@dataclass
class CompetitionResult:
    keyword: str
    product_count: int       # 네이버 쇼핑 등록 상품 총 수 (total)
    barrier: str             # 'low' | 'mid' | 'high'
    top_prices: List[int] = field(default_factory=list)  # 상위 상품 가격 (판매가 참고용)
    avg_price: float = 0.0   # 상위 상품 평균 판매가 (참고용 — 리뷰·신뢰 쌓인 기존셀러 가격대)
    entry_price: float = 0.0  # 신규셀러 예상 진입가 (마진 계산 기준가) — 상위가격 하위 1/3 지점
    top_titles: List[str] = field(default_factory=list)  # 상위 상품명 (도매매 타입매칭용)
    top_mall_names: List[str] = field(default_factory=list)  # 상위 상품 판매처명 (과점도 판단용)

    def summary(self) -> str:
        barrier_str = {
            "low": "약함 (경쟁지수 낮음, 진입 쉬움)",
            "mid": "보통 (경쟁지수 중간, SEO 최적화 필요)",
            "high": "강함 (경쟁지수 높음, 진입 어려움)",
        }.get(self.barrier, "?")
        return f"경쟁 강도: {barrier_str}"


def fetch_competition(keyword: str, comp_idx: str = "") -> CompetitionResult:
    """
    검색광고 API 경쟁지수(comp_idx)로 경쟁 강도 근사. 네트워크 호출 없음
    (comp_idx는 호출측이 keyword_tool.discover_keywords()로 이미 확보한 값을 전달).
    """
    return CompetitionResult(
        keyword=keyword,
        product_count=0,
        barrier=_COMP_IDX_TO_BARRIER.get(comp_idx, "mid"),
    )

