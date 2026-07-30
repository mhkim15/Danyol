"""
소싱 자동 탐색 파이프라인.

흐름:
  1. 데이터랩 쇼핑인사이트 → 카테고리 인기 키워드 자동 수집
  2. 네이버 쇼핑 검색 API → 각 키워드 등록 상품수 + 평균 판매가
  3. 데이터랩 쇼핑인사이트 → 키워드별 트렌드 방향 (상승/안정/하락)
  4. 도매매 DB API → 최저 도매가 조회
  5. 마진 계산기 → 2026 수수료 기준 실질 마진율
  6. 종합 점수 산출 → 보고서 출력

사용법:
  python3 main.py sourcing discover --category 주방용품 --limit 20
  python3 main.py sourcing discover --keywords data/keywords.txt --category 주방용품
"""
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import (
    GOLDEN_RATIO,
    SUPPLY_CEILING,
    SUPPLY_TIERS,
    RESEED_MIN_SEARCH,
    CANDIDATE_CAP,
    EXPANSION_DEPTH,
    TARGET_CATEGORIES,
    BLOCKED_BRAND_PREFIXES,
    BLOCKED_ELECTRIC_KEYWORDS,
    BLOCKED_MEDICAL_KEYWORDS,
    BLOCKED_KIDS_KEYWORDS,
    BLOCKED_INFO_INTENT_SUFFIXES,
    BLOCKED_LOCATION_PREFIXES,
    BLOCKED_GENDER_PREFIXES,
    REMAKE_MIN_SEARCH,
    REMAKE_SUPPLY_FLOOR,
    REMAKE_PRICE_SPREAD_MIN,
    REMAKE_SELLER_RATIO_MIN,
)
from ..margin.calculator import calculate as calc_margin
from .competition import CompetitionResult, fetch_competition
from .domemae import search_products, find_matching_product
from .keyword_tool import KeywordData, discover_keywords
from .models import ProductCandidate
from .trend import TrendResult, fetch_trend


# ── 점수 기준 ─────────────────────────────────────────────────────────────────

def _supply_tier(product_count: int) -> str:
    """등록 상품수 → 진입 난이도 tier (tight/normal/loose/over)."""
    if product_count <= SUPPLY_TIERS["tight"]:
        return "tight"
    if product_count <= SUPPLY_TIERS["normal"]:
        return "normal"
    if product_count <= SUPPLY_TIERS["loose"]:
        return "loose"
    return "over"


\
# ── 3축 스코어링 (수요 × 진입가능성 × 수익성) ─────────────────────────────────
#
# 골든레이시오(검색수÷상품수) 하나로 기본점수 대부분을 결정하던 방식은
# (1) 상품수가 "경쟁자 강도"를 반영 못 하고 "경쟁자 수"만 본다는 점,
# (2) 분자/분모가 둘 다 작을 때 비율이 착시(가짜 블루오션)를 만든다는 점,
# (3) 비율 연산이 "선형으로 좋다"는 잘못된 가정을 깐다는 점에서 구조적 한계가
# 있었다. 대신 수요/진입가능성/수익성을 독립 축으로 점수화한 뒤 트랙별로
# 가중 평균한다. 골든레이시오는 폐기하지 않고 참고 보조지표로만 남긴다
# (2026-07-29 재설계).
#
# 트랙 A(신규 틈새 발굴) — 상품수 적을수록 가점, SUPPLY_CEILING 하드컷 유지.
# 트랙 B(리메이크 후보) — 상품수 하한만 게이트, 절대 검색량·가격분산·판매처
# 파편화를 봐서 "이미 풀린 시장이어도 리메이크로 이길 여지"를 찾는다.

def _demand_score(monthly_search: int, trend: TrendResult, keyword_data: Optional[KeywordData],
                   track: str) -> int:
    """수요 축 (0~100): 절대 검색량 구간 + 트렌드 방향 + 계절성 + 클릭률."""
    if track == "A":
        if monthly_search >= 10_000:
            score = 60
        elif monthly_search >= 5_000:
            score = 45
        elif monthly_search >= RESEED_MIN_SEARCH:
            score = 30
        elif monthly_search > 0:
            score = 15
        else:
            score = 0
        score += {"up": 20, "stable": 8, "down": -15}.get(trend.direction, 0)
    else:  # 트랙 B — 절대 검색량 자체가 핵심, 트렌드 상승에 더 큰 가중치
        if monthly_search >= 20_000:
            score = 55
        elif monthly_search >= 10_000:
            score = 40
        elif monthly_search >= REMAKE_MIN_SEARCH:
            score = 25
        else:
            score = 0
        score += {"up": 30, "stable": 10, "down": -25}.get(trend.direction, 0)

    if trend.is_seasonal:
        score -= 10

    click_rate = getattr(keyword_data, "click_rate", 0.0) if keyword_data else 0.0
    if click_rate >= 0.30:
        score += 10

    return max(0, min(100, score))


def _entry_score(
    product_count: int,
    price_spread: float,
    unique_seller_ratio: float,
    has_seller_data: bool,
    track: str,
    supply_matched: Optional[bool] = None,
) -> int:
    """
    진입가능성 축 (0~100): 상품수(트랙 A만) + 가격분산도 + 판매처 파편화 + 도매매 매칭.

    supply_matched: 도매매 조회 결과 반영 (None=아직 미조회/중립, True=형태 일치
    확인됨, False=매칭 실패 또는 불확실). 검색 수요는 있어도 실제로 소싱할 수
    있는 도매매 상품이 없으면 진입가능성이 낮다고 봐야 하는데, 기존에는 이
    정보가 점수에 전혀 반영되지 않고 마진 강제 제외 규칙에만 쓰였다 — 도매매
    조회가 늦게(상위 후보에서만) 일어나는 구조상 어쩔 수 없이 점수 재계산
    시점에 반영한다 (2026-07-30).
    """
    if track == "A":
        # 상품수 적을수록 진입 쉬움 — 기존 supply tier 철학 계승
        if product_count <= SUPPLY_TIERS["tight"]:
            score = 55
        elif product_count <= SUPPLY_TIERS["normal"]:
            score = 40
        elif product_count <= SUPPLY_TIERS["loose"]:
            score = 20
        else:
            score = 0
    else:
        # 트랙 B — 상품수는 참고만(시장 규모 신호), 과점 여부가 핵심
        score = 30 if product_count >= REMAKE_SUPPLY_FLOOR else 0

    # 가격 분산도 — 넓을수록 품질/브랜드 차별화 여지 (두 트랙 공통 가점)
    if price_spread >= REMAKE_PRICE_SPREAD_MIN:
        score += 25
    elif price_spread >= REMAKE_PRICE_SPREAD_MIN / 2:
        score += 12

    # 판매처 파편화 — 소수 판매처 과점이면 감점, 파편화돼 있으면 가점
    if has_seller_data:
        if unique_seller_ratio >= REMAKE_SELLER_RATIO_MIN:
            score += 20
        elif unique_seller_ratio < 0.3:
            score -= 15

    # 도매매 매칭 — 실제 소싱 가능성이 검증됐는지
    if supply_matched is True:
        score += 15
    elif supply_matched is False:
        score -= 20

    return max(0, min(100, score))


def _profit_score(margin_passes: bool, margin_rate: float) -> int:
    """수익성 축 (0~100): 마진 목표 통과 여부. 도매가 미조회 시 중립(50점)."""
    if margin_rate <= 0:
        return 50  # 아직 도매가/마진 미조회 — 판단 보류(중립)
    return 100 if margin_passes else 20


# 트랙별 3축 가중치 — 합 1.0
_TRACK_A_WEIGHTS = {"demand": 0.35, "entry": 0.45, "profit": 0.20}
_TRACK_B_WEIGHTS = {"demand": 0.45, "entry": 0.35, "profit": 0.20}


def _score_from_axes(
    product_count: int,
    price_spread: float,
    unique_seller_ratio: float,
    has_seller_data: bool,
    monthly_search: int,
    trend: TrendResult,
    keyword_data: Optional[KeywordData] = None,
    margin_passes: bool = False,
    margin_rate: float = 0.0,
    track: str = "A",
    supply_matched: Optional[bool] = None,
) -> int:
    """수요×진입가능성×수익성 3축 가중평균 점수 (0~100점) — 원시값 기반."""
    if product_count <= 0:
        return 0
    if track == "A" and product_count > SUPPLY_CEILING:
        return 0
    if track == "B" and product_count < REMAKE_SUPPLY_FLOOR:
        return 0
    if monthly_search > 0:
        min_search = REMAKE_MIN_SEARCH if track == "B" else RESEED_MIN_SEARCH
        if monthly_search < min_search:
            return 0

    weights = _TRACK_B_WEIGHTS if track == "B" else _TRACK_A_WEIGHTS
    demand = _demand_score(monthly_search, trend, keyword_data, track)
    entry = _entry_score(
        product_count, price_spread, unique_seller_ratio, has_seller_data, track,
        supply_matched=supply_matched,
    )
    profit = _profit_score(margin_passes, margin_rate)

    total = demand * weights["demand"] + entry * weights["entry"] + profit * weights["profit"]
    return max(0, min(100, round(total)))


def _score(
    competition: CompetitionResult,
    trend: TrendResult,
    keyword_data: Optional[KeywordData] = None,
    margin_passes: bool = False,
    margin_rate: float = 0.0,
    track: str = "A",
) -> int:
    """CompetitionResult 기반 래퍼 — 최초 채점(1차 조회 직후)에 사용."""
    monthly_search = getattr(keyword_data, "monthly_total", 0) if keyword_data else 0
    return _score_from_axes(
        product_count=competition.product_count,
        price_spread=competition.price_spread,
        unique_seller_ratio=competition.unique_seller_ratio,
        has_seller_data=bool(competition.top_mall_names),
        monthly_search=monthly_search,
        trend=trend, keyword_data=keyword_data,
        margin_passes=margin_passes, margin_rate=margin_rate, track=track,
    )


def _recommendation(score: int, tier: str, track: str = "A") -> str:
    if track == "B":
        if score >= 60:
            return "리메이크 권장"
        if score >= 40:
            return "리메이크 검토"
        return "제외"

    if tier == "over":
        return "제외"
    if score >= 55:
        return "진입 권장"
    if score >= 40:
        return "진입 가능"
    if score >= 25:
        return "보류"
    return "제외"


# ── 결과 모델 ─────────────────────────────────────────────────────────────────

@dataclass
class DiscoveryResult:
    keyword: str
    category: str
    score: int
    recommendation: str
    product_count: int                            # 네이버 쇼핑 등록 상품수
    monthly_search: int                           # 실제 월간 검색수 (광고 API)
    trend_direction: str                          # up / stable / down
    is_seasonal: bool
    competition_barrier: str                      # low / mid / high
    supply_tier: str                              # tight / normal / loose / over
    avg_naver_price: float                        # 네이버 상위 평균 판매가
    supply_price: int                             # 도매매 최저 도매가 (0 = 조회 실패)
    supply_name: str                              # 도매매 상품명
    margin_rate: float                            # 실질 마진율 (0 = 계산 불가)
    margin_passes: bool                           # 목표 마진율(20%) + 절대이익 하한 동시 달성 여부
    trend_index: float = 0.0
    error: str = ""
    top_titles: List[str] = field(default_factory=list)  # 네이버 상위 상품명 (타입매칭용)
    supply_match_uncertain: bool = False          # True면 도매매 상품 타입 일치 미확인
    entry_price: float = 0.0                      # 신규셀러 예상 진입가 (마진 계산 기준가)
    track: str = "A"                              # "A"=신규 틈새 발굴 / "B"=리메이크 후보
    price_spread: float = 0.0                     # (최고가-최저가)/평균가 — 가격 스펙트럼
    unique_seller_ratio: float = 0.0               # 고유판매처수/상위노출수 — 낮을수록 과점
    has_seller_data: bool = False                  # mallName 조회 성공 여부 (재계산 시 판별용)

    @property
    def golden_ratio(self) -> float:
        if self.product_count == 0:
            return 0.0
        return round(self.monthly_search / self.product_count, 2)

    def one_line(self) -> str:
        trend_ko = {"up": "상승", "stable": "안정", "down": "하락"}.get(self.trend_direction, "-")
        barrier_ko = {"low": "약함", "mid": "보통", "high": "강함"}.get(self.competition_barrier, "-")
        margin_str = f"{self.margin_rate:.1%}" if self.margin_rate else "미조회"
        seasonal = " [계절성]" if self.is_seasonal else ""
        uncertain = " ⚠매칭불확실" if self.supply_match_uncertain else ""
        return (
            f"  {self.keyword:<18} 점수:{self.score:>3}  "
            f"트렌드:{trend_ko:<3}  경쟁:{barrier_ko:<3}  "
            f"상품수:{self.product_count:>6,}개  "
            f"도매가:{self.supply_price:>7,}원{uncertain}  마진:{margin_str}  "
            f"→ {self.recommendation}{seasonal}"
        )

    def detail(self) -> str:
        lines = [
            f"\n{'─'*60}",
            f"  [{self.recommendation}] {self.keyword}  (점수: {self.score}/100)",
            f"{'─'*60}",
            f"  트렌드   : {'상승' if self.trend_direction == 'up' else '안정' if self.trend_direction == 'stable' else '하락'}"
            + (f" (지수: {self.trend_index:.1f})" if self.trend_index else "")
            + (" ⚠ 계절성 상품" if self.is_seasonal else ""),
            f"  경쟁강도 : {'약함 ✓' if self.competition_barrier == 'low' else '보통' if self.competition_barrier == 'mid' else '강함 ✗'}  (등록 상품: {self.product_count:,}개)",
            f"  네이버가 : 상위 평균 {self.avg_naver_price:,.0f}원  |  신규셀러 진입가(추정) {self.entry_price:,.0f}원",
        ]
        if self.track == "B":
            seller_str = f"{self.unique_seller_ratio:.0%}" if self.has_seller_data else "미조회"
            lines.append(
                f"  진입가능성: 가격분산 {self.price_spread:.0%}  |  판매처다양성 {seller_str}"
            )
        if self.supply_price:
            match_flag = "  ⚠ 상품타입 일치 미확인 — 실물 수동확인 필수" if self.supply_match_uncertain else ""
            lines.append(f"  도매가   : {self.supply_price:,}원  ({self.supply_name[:30]}){match_flag}")
            if self.margin_rate:
                margin_flag = "✓ 목표달성" if self.margin_passes else "△ 목표미달(마진 또는 절대이익 부족)"
                lines.append(f"  마진율   : {self.margin_rate:.1%}  (진입가 {self.entry_price:,.0f}원 기준)  {margin_flag}")
        else:
            lines.append("  도매가   : 조회 실패 (도매매 수동 확인 필요)")
        if self.error:
            lines.append(f"  오류     : {self.error}")
        return "\n".join(lines)


# ── 파이프라인 ─────────────────────────────────────────────────────────────────

def _prefilter_candidates(kds: List[KeywordData], cap: int) -> List[KeywordData]:
    """
    상품수 조회(쇼핑 API) 전 1차 거름 — 호출 폭발 방지.
      - monthly_clicks == 0 (검색만 있고 클릭 0인 정보성) 제외
    남은 후보를 사전점수(검색수 × 클릭률 보정, comp_idx '높음'은 감점)로
    정렬해 상위 cap개만 반환.

    comp_idx '높음'은 예전엔 하드 제외였다("광고 포화 = 경쟁 큼"). 그런데
    실측 확인 결과(2026-07) 소규모 카테고리(메이크업도구·헤어케어 등)는
    원시 후보 자체가 5~6개뿐이고 그중 대부분이 comp_idx 높음이라, 하드
    제외 시 카테고리 전체가 후보 0개로 붕괴했다. 광고 경쟁지수가 높다는 건
    "광고주가 그 키워드에 돈을 쓸 만큼 전환이 확실하다"는 신호이기도 해서
    무조건 나쁜 신호로 보기 어렵다 — 감점 요소로 낮춰서 후보 풀에는 남긴다.
    """
    def _is_brand_keyword(kw: str) -> bool:
        kw_lower = kw.lower()
        return any(b.lower() in kw_lower for b in BLOCKED_BRAND_PREFIXES)

    def _is_electric_keyword(kw: str) -> bool:
        kw_lower = kw.lower()
        return any(e.lower() in kw_lower for e in BLOCKED_ELECTRIC_KEYWORDS)

    def _is_medical_keyword(kw: str) -> bool:
        kw_lower = kw.lower()
        return any(m.lower() in kw_lower for m in BLOCKED_MEDICAL_KEYWORDS)

    def _is_kids_keyword(kw: str) -> bool:
        return any(k in kw for k in BLOCKED_KIDS_KEYWORDS)

    def _is_info_intent_keyword(kw: str) -> bool:
        return any(s in kw for s in BLOCKED_INFO_INTENT_SUFFIXES)

    def _is_location_keyword(kw: str) -> bool:
        return any(loc in kw for loc in BLOCKED_LOCATION_PREFIXES)

    def _is_gender_mismatch_keyword(kw: str) -> bool:
        return any(g in kw for g in BLOCKED_GENDER_PREFIXES)

    pool = [
        kd for kd in kds
        if kd.monthly_clicks > 0
        and not _is_brand_keyword(kd.keyword)
        and not _is_electric_keyword(kd.keyword)
        and not _is_medical_keyword(kd.keyword)
        and not _is_kids_keyword(kd.keyword)
        and not _is_info_intent_keyword(kd.keyword)
        and not _is_location_keyword(kd.keyword)
        and not _is_gender_mismatch_keyword(kd.keyword)
    ]
    # 사전점수: 검색수에 클릭률(구매의도) 가중, comp_idx 높음은 감점(제외 아님).
    def _pre(kd: KeywordData) -> float:
        base = kd.monthly_total * (1.0 + kd.click_rate)
        return base * 0.5 if kd.comp_idx == "높음" else base
    pool.sort(key=_pre, reverse=True)
    return pool[:cap]


def discover(
    category: str,
    keywords: Optional[List[str]] = None,
    limit: int = 20,
    min_score: int = 0,
    with_domemae: bool = True,
    api_delay: float = 0.3,
    depth: int = EXPANSION_DEPTH,
    max_supply: int = SUPPLY_CEILING,
    domemae_top_n: int = 15,
    tracks: Optional[List[str]] = None,
) -> List[DiscoveryResult]:
    """
    소싱 탐색 파이프라인 — 트랙 A(신규 틈새 발굴) + 트랙 B(리메이크 후보) 병행.

    같은 키워드 후보 풀에서 두 트랙을 각각 채점한다.
    트랙 A: 상품수 적을수록 유리(SUPPLY_CEILING 하드컷), 골든레이시오 계열 철학 계승.
    트랙 B: 상품수 많아도 무방(REMAKE_SUPPLY_FLOOR 하한만), 절대 검색량·가격분산·
            판매처 파편화로 "이미 풀린 시장이어도 리메이크로 이길 여지"를 찾는다.

    Args:
        category      : 소싱 카테고리 (예: 주방용품)
        keywords      : 직접 지정 키워드 목록. None이면 재귀 확장 자동 발굴
        limit         : 트랙별 최종 결과 최대 개수
        min_score     : 이 점수 이상만 결과에 포함 (0 = 전체)
        with_domemae  : 도매매 도매가 조회 여부 (상위 도매매_top_n개만)
        api_delay     : API 호출 간 딜레이(초)
        depth         : 키워드 재귀 확장 단계 수
        max_supply    : 트랙 A 등록 상품수 하드 상한 (초과 시 트랙 A 후보 제외)
        domemae_top_n : 도매가 조회할 상위 후보 수 (트랙별)
        tracks        : 실행할 트랙 목록. None이면 ["A", "B"] 둘 다 실행

    Returns:
        DiscoveryResult 리스트 (트랙 A 결과 다음 트랙 B 결과 순, 각 트랙 내부는 점수 내림차순)
    """
    tracks = tracks or ["A", "B"]

    # ── Step 1: 키워드 수집 (재귀 확장) — 트랙 공통 ──────────────────────────
    if keywords:
        kd_list = [KeywordData(keyword=k.strip(), monthly_pc=0, monthly_mobile=0)
                   for k in keywords if k.strip()]
        print(f"  키워드 {len(kd_list)}개 직접 입력")
    else:
        print(f"  [{category}] 롱테일 재귀 발굴 중 (depth={depth})...")
        try:
            kd_list = discover_keywords(category, depth=depth)
            print(f"  → 후보 {len(kd_list)}개 발굴")
        except Exception as e:
            print(f"  [경고] 키워드 자동 탐색 실패: {e}")
            print("  --keywords 옵션으로 키워드를 직접 입력하세요.")
            return []

    # ── 사전 필터 + 후보 캡 (쇼핑 API 호출 제한) — 트랙 공통 ─────────────────
    # 브랜드/전기/의료/아동/정보성/지역명 차단은 법적 리스크 회피 목적이라
    # 리메이크 트랙에도 동일 적용한다.
    if not keywords:
        before = len(kd_list)
        kd_list = _prefilter_candidates(kd_list, CANDIDATE_CAP)
        print(f"  → 사전 필터 후 {len(kd_list)}개 (제외 {before - len(kd_list)}개)")

    keyword_data_map = {kd.keyword: kd for kd in kd_list}
    keyword_list = [kd.keyword for kd in kd_list]

    # ── Step 2~3: 상품수 + 트렌드 조회 — 트랙 공통(API 호출 1회로 공유) ───────
    fetched = []  # (keyword, kd, competition, trend, error_msgs)
    for i, keyword in enumerate(keyword_list, 1):
        print(f"  [{i}/{len(keyword_list)}] '{keyword}' 조회 중...", end=" ", flush=True)
        kd = keyword_data_map.get(keyword)
        error_msgs = []

        try:
            competition = fetch_competition(keyword)
            time.sleep(api_delay)
        except Exception as e:
            error_msgs.append(f"경쟁도조회실패:{e}")
            competition = CompetitionResult(keyword=keyword, product_count=0, barrier="mid")

        if competition.product_count <= 0:
            print("제외 (상품수 조회 실패)")
            continue

        try:
            trend = fetch_trend(keyword)
            time.sleep(api_delay)
        except Exception as e:
            error_msgs.append(f"트렌드조회실패:{e}")
            trend = TrendResult(keyword=keyword, direction="stable",
                                recent_avg=0, prev_avg=0, variance=0,
                                is_seasonal=False)

        print(f"상품수:{competition.product_count:,} 검색:{kd.monthly_total if kd else 0:,}")
        fetched.append((keyword, kd, competition, trend, error_msgs))

    # ── Step 4~6: 트랙별 채점 + 도매매/마진 검증 + 등급 확정 ──────────────────
    all_results: List[DiscoveryResult] = []
    do_domemae = with_domemae and os.environ.get("DOMEMAE_API_KEY")
    if with_domemae and not os.environ.get("DOMEMAE_API_KEY"):
        print("  [안내] DOMEMAE_API_KEY 미설정 — 도매가/마진 생략")

    for track in tracks:
        track_max_supply = max_supply if track == "A" else None
        results = []
        for keyword, kd, competition, trend, error_msgs in fetched:
            if track == "A" and competition.product_count > track_max_supply:
                continue
            if track == "B" and competition.product_count < REMAKE_SUPPLY_FLOOR:
                continue

            score = _score(competition, trend, kd, margin_passes=False, track=track)
            if score <= 0:
                continue

            tier = _supply_tier(competition.product_count)
            monthly_search = kd.monthly_total if kd else 0

            result = DiscoveryResult(
                keyword=keyword,
                category=category,
                score=score,
                recommendation="",   # 마진 반영 후 확정
                product_count=competition.product_count,
                monthly_search=monthly_search,
                trend_direction=trend.direction,
                is_seasonal=trend.is_seasonal,
                competition_barrier=competition.barrier,
                supply_tier=tier,
                avg_naver_price=competition.avg_price,
                supply_price=0,
                supply_name="",
                margin_rate=0.0,
                margin_passes=False,
                trend_index=kd.ratio if kd and hasattr(kd, "ratio") else trend.recent_avg,
                error="; ".join(error_msgs),
                top_titles=competition.top_titles,
                entry_price=competition.entry_price,
                track=track,
                price_spread=competition.price_spread,
                unique_seller_ratio=competition.unique_seller_ratio,
                has_seller_data=bool(competition.top_mall_names),
            )
            result._trend = trend
            result._kd = kd
            results.append(result)

        # 점수 우선 정렬 → 상위 N개만 도매매/마진 검증
        results.sort(key=lambda r: r.score, reverse=True)

        prelim_top, prelim_rest = [], []
        for r in results:
            prelim = _recommendation(r.score, r.supply_tier, track=track)
            top_label = "진입 권장" if track == "A" else "리메이크 권장"
            (prelim_top if prelim == top_label else prelim_rest).append(r)

        domemae_targets = (
            prelim_top[:domemae_top_n] + prelim_rest[:domemae_top_n]
            if do_domemae else []
        )

        for r in domemae_targets:
            supply_matched: Optional[bool] = None
            try:
                domemae_result = search_products(r.keyword, limit=10)
                p, matched = find_matching_product(r.top_titles, domemae_result.products)
                if p:
                    r.supply_price = p.supply_price
                    r.supply_name = p.name
                    r.supply_match_uncertain = not matched
                    supply_matched = matched
                else:
                    # 도매매 검색 자체는 됐는데 매칭 후보가 하나도 없음 — 소싱 불확실
                    supply_matched = False
                time.sleep(api_delay)
            except Exception as e:
                r.error = (r.error + f"; 도매매조회실패:{e}").strip("; ")

            if r.supply_price and r.entry_price:
                try:
                    margin_result = calc_margin(
                        sale_price=int(r.entry_price),
                        cost_price=r.supply_price,
                        free_shipping=(r.entry_price >= 30_000),
                    )
                    r.margin_rate = margin_result.margin_rate
                    r.margin_passes = margin_result.passes_target and margin_result.passes_abs_floor
                except Exception as e:
                    r.error = (r.error + f"; 마진계산실패:{e}").strip("; ")

            if supply_matched is not None:
                r.score = _score_from_axes(
                    product_count=r.product_count,
                    price_spread=r.price_spread,
                    unique_seller_ratio=r.unique_seller_ratio,
                    has_seller_data=r.has_seller_data,
                    monthly_search=r.monthly_search,
                    trend=r._trend, keyword_data=r._kd,
                    margin_passes=r.margin_passes, margin_rate=r.margin_rate,
                    track=track, supply_matched=supply_matched,
                )

        for r in results:
            r.recommendation = _recommendation(r.score, r.supply_tier, track=track)
            if r.supply_price and r.margin_rate and not r.margin_passes:
                r.recommendation = "제외"

        results.sort(key=lambda r: r.score, reverse=True)
        if min_score > 0:
            results = [r for r in results if r.score >= min_score]

        all_results.extend(results[:limit] if limit else results)

    return all_results


def to_product_candidates(results: List[DiscoveryResult]) -> List[ProductCandidate]:
    """DiscoveryResult → ProductCandidate 변환 (sourcing_log.json 저장용)."""
    candidates = []
    for r in results:
        c = ProductCandidate(
            keyword=r.keyword,
            monthly_search=0,            # 데이터랩 상대 지수 사용 — 절대값 미제공
            product_count=r.product_count,
            category=r.category,
            is_seasonal=r.is_seasonal,
            notes=(
                f"자동탐색 | 트렌드:{r.trend_direction} | 경쟁:{r.competition_barrier}"
                + (f" | 도매가:{r.supply_price:,}원" if r.supply_price else "")
                + (f" | 마진:{r.margin_rate:.1%}" if r.margin_rate else "")
            ),
            est_sale_price=int(r.avg_naver_price) if r.avg_naver_price else 0,
            est_cost_price=r.supply_price,
        )
        c.score = r.score
        candidates.append(c)
    return candidates


def _print_track_table(results: List[DiscoveryResult], category: str, track: str) -> None:
    tier_ko = {"tight": "엄격", "normal": "보통", "loose": "느슨", "over": "초과"}
    if track == "A":
        print(f"\n{'─'*104}")
        print(f"  {'키워드':<18} {'카테고리':<10} {'점수':>4}  {'트렌드':<5} {'검색수':>7}  {'상품수':>8}  {'공급':<5} {'레이시오':>7}  판단")
        print(f"{'─'*104}")
        for r in results:
            trend_ko = {"up": "상승", "stable": "안정", "down": "하락"}.get(r.trend_direction, "-")
            seasonal = "★" if r.is_seasonal else " "
            search_str = f"{r.monthly_search:,}" if r.monthly_search else "  -  "
            golden_str = f"{r.golden_ratio:.1f}" if r.golden_ratio else "  -  "
            tier_str = tier_ko.get(r.supply_tier, "-")
            cat_str = getattr(r, "category", category)
            print(
                f"  {r.keyword:<18} {cat_str:<10} {r.score:>4}  {trend_ko:<5} {search_str:>7}  "
                f"{r.product_count:>8,}개  {tier_str:<5} {golden_str:>7}  {seasonal}{r.recommendation}"
            )
    else:
        print(f"\n{'─'*110}")
        print(f"  {'키워드':<18} {'카테고리':<10} {'점수':>4}  {'트렌드':<5} {'검색수':>7}  {'상품수':>8}  {'가격분산':>8}  {'판매처다양성':>10}  판단")
        print(f"{'─'*110}")
        for r in results:
            trend_ko = {"up": "상승", "stable": "안정", "down": "하락"}.get(r.trend_direction, "-")
            seasonal = "★" if r.is_seasonal else " "
            search_str = f"{r.monthly_search:,}" if r.monthly_search else "  -  "
            spread_str = f"{r.price_spread:.0%}" if r.price_spread else "  -  "
            seller_str = f"{r.unique_seller_ratio:.0%}" if r.has_seller_data else "  -  "
            cat_str = getattr(r, "category", category)
            print(
                f"  {r.keyword:<18} {cat_str:<10} {r.score:>4}  {trend_ko:<5} {search_str:>7}  "
                f"{r.product_count:>8,}개  {spread_str:>8}  {seller_str:>10}  {seasonal}{r.recommendation}"
            )


def print_report(results: List[DiscoveryResult], category: str) -> None:
    """터미널 보고서 출력 — 트랙 A(신규 틈새)와 트랙 B(리메이크 후보) 섹션 분리."""
    if not results:
        print("\n분석 결과가 없습니다.")
        return

    track_a = [r for r in results if r.track == "A"]
    track_b = [r for r in results if r.track == "B"]

    print(f"\n{'═'*70}")
    print(f"  소싱 탐색 보고서 — [{category}]  (총 {len(results)}개, 트랙A {len(track_a)} / 트랙B {len(track_b)})")
    print(f"{'═'*70}")

    # ── 트랙 A: 신규 틈새 발굴 ──────────────────────────────────────────────
    if track_a:
        recommended = [r for r in track_a if r.recommendation == "진입 권장"]
        possible    = [r for r in track_a if r.recommendation == "진입 가능"]
        hold        = [r for r in track_a if r.recommendation in ("보류", "제외")]

        print(f"\n{'▓'*70}")
        print("  [트랙 A] 신규 틈새 발굴 — 경쟁 적고 수요 있는 블루오션")
        print(f"{'▓'*70}")
        _print_track_table(track_a, category, "A")

        summary_parts = []
        if recommended:
            summary_parts.append(f"진입 권장 {len(recommended)}개")
        if possible:
            summary_parts.append(f"진입 가능 {len(possible)}개")
        if hold:
            summary_parts.append(f"보류/제외 {len(hold)}개")
        print(f"\n  결과: {' · '.join(summary_parts)}")

        if recommended:
            print(f"\n{'━'*70}")
            print("  [트랙 A: 진입 권장 — 상세 분석]")
            for r in recommended:
                print(r.detail())
        if possible:
            print(f"\n{'━'*70}")
            print("  [트랙 A: 진입 가능 — 상세 분석]")
            for r in possible:
                print(r.detail())

    # ── 트랙 B: 리메이크 후보 ───────────────────────────────────────────────
    if track_b:
        remake_recommended = [r for r in track_b if r.recommendation == "리메이크 권장"]
        remake_review      = [r for r in track_b if r.recommendation == "리메이크 검토"]

        print(f"\n{'▓'*70}")
        print("  [트랙 B] 리메이크 후보 — 이미 풀린 시장이어도 리메이크로 경쟁 가능")
        print(f"{'▓'*70}")
        _print_track_table(track_b, category, "B")

        summary_parts = []
        if remake_recommended:
            summary_parts.append(f"리메이크 권장 {len(remake_recommended)}개")
        if remake_review:
            summary_parts.append(f"리메이크 검토 {len(remake_review)}개")
        print(f"\n  결과: {' · '.join(summary_parts) if summary_parts else '해당 없음'}")

        if remake_recommended:
            print(f"\n{'━'*70}")
            print("  [트랙 B: 리메이크 권장 — 상세 분석]")
            for r in remake_recommended:
                print(r.detail())
        if remake_review:
            print(f"\n{'━'*70}")
            print("  [트랙 B: 리메이크 검토 — 상세 분석]")
            for r in remake_review:
                print(r.detail())

    print(f"\n{'═'*70}")
    print("  ★ = 계절성 상품 (비수기 재고 리스크 주의)")
    print("  [트랙 A] 공급: 엄격(~3천)·보통(~1만)·느슨(~3만) = 등록 상품수 구간 (적을수록 블루오션)")
    print("           레이시오 = 월검색수 ÷ 등록상품수 (참고 보조지표, 높을수록 수요>공급)")
    print("  [트랙 B] 가격분산 = (최고가-최저가)/평균가 (높을수록 리메이크로 파고들 여지)")
    print("           판매처다양성 = 상위 노출 고유 판매처 비율 (높을수록 과점 아님, 진입 여지)")
    print("  다음 단계: 권장 등급 상품을 도매꾹/온채널에서 수동 최종 확인")
    print(f"{'═'*70}\n")


# ── 전 카테고리 비교 스캔 ──────────────────────────────────────────────────────

@dataclass
class CategoryScore:
    category: str
    results: List[DiscoveryResult]

    @property
    def recommended(self) -> int:
        return sum(1 for r in self.results if r.recommendation == "진입 권장")

    @property
    def possible(self) -> int:
        return sum(1 for r in self.results if r.recommendation == "진입 가능")

    @property
    def analyzed(self) -> int:
        return len(self.results)

    @property
    def opportunity_density(self) -> float:
        """기회 밀도 = (진입 권장+가능) / 분석 후보 수."""
        if not self.results:
            return 0.0
        return (self.recommended + self.possible) / len(self.results)

    @property
    def avg_golden(self) -> float:
        ratios = [r.golden_ratio for r in self.results if r.golden_ratio]
        return round(sum(ratios) / len(ratios), 2) if ratios else 0.0

    @property
    def median_supply(self) -> int:
        counts = sorted(r.product_count for r in self.results if r.product_count)
        return counts[len(counts) // 2] if counts else 0

    @property
    def top3(self) -> List[DiscoveryResult]:
        return self.results[:3]


def scan_categories(
    categories: Optional[List[str]] = None,
    limit: int = 30,
    with_domemae: bool = False,
    depth: int = EXPANSION_DEPTH,
    max_supply: int = SUPPLY_CEILING,
) -> List[CategoryScore]:
    """
    여러 카테고리를 동일 엔진으로 스캔해 카테고리별 경쟁력(기회 밀도)을 비교.
    비교 단계에서는 도매가 조회 기본 생략(with_domemae=False)으로 비용 절약.
    트랙 A(신규 틈새) 기준으로만 비교한다 — 기회밀도/레이시오 지표가 트랙 A 철학이라
    트랙 B를 섞으면 지표가 왜곡된다.
    """
    cats = categories or list(TARGET_CATEGORIES)
    scores: List[CategoryScore] = []
    for cat in cats:
        print(f"\n{'#'*70}\n  [{cat}] 스캔 시작\n{'#'*70}")
        try:
            results = discover(
                category=cat, limit=limit, with_domemae=with_domemae,
                depth=depth, max_supply=max_supply, tracks=["A"],
            )
        except Exception as e:
            print(f"  [경고] '{cat}' 스캔 실패: {e}")
            results = []
        scores.append(CategoryScore(category=cat, results=results))
    return scores


def print_category_ranking(scores: List[CategoryScore]) -> None:
    """카테고리 순위표 — 기회 밀도·평균 레이시오·대표 키워드 비교."""
    # 정렬: 기회 밀도 → 평균 레이시오
    ranked = sorted(
        scores,
        key=lambda s: (s.opportunity_density, s.avg_golden),
        reverse=True,
    )

    print(f"\n{'═'*78}")
    print("  카테고리 경쟁력 비교 — 어디로 진입할까")
    print(f"{'═'*78}")
    print(f"\n  {'순위':<4} {'카테고리':<10} {'기회밀도':>7}  {'권장':>4} {'가능':>4}  {'평균레이시오':>10}  {'중앙상품수':>9}")
    print(f"  {'─'*70}")
    for rank, s in enumerate(ranked, 1):
        print(
            f"  {rank:<4} {s.category:<10} {s.opportunity_density:>6.0%}  "
            f"{s.recommended:>4} {s.possible:>4}  {s.avg_golden:>10.2f}  {s.median_supply:>8,}개"
        )
    print(f"  {'─'*70}")

    print(f"\n{'━'*78}")
    print("  카테고리별 대표 블루오션 키워드 (레이시오 상위 3)")
    print(f"{'━'*78}")
    for s in ranked:
        print(f"\n  ◆ {s.category}  (분석 {s.analyzed}개)")
        if not s.top3:
            print("     - 후보 없음")
            continue
        for r in s.top3:
            seasonal = " ★" if r.is_seasonal else ""
            print(
                f"     - {r.keyword:<18} 레이시오 {r.golden_ratio:>5.1f}  "
                f"검색 {r.monthly_search:>6,}  상품 {r.product_count:>7,}개  "
                f"[{r.recommendation}]{seasonal}"
            )

    if ranked:
        best = ranked[0]
        print(f"\n{'═'*78}")
        print(f"  추천: '{best.category}' — 기회밀도 {best.opportunity_density:.0%}, 평균 레이시오 {best.avg_golden:.2f}")
        print("  다음 단계: 해당 카테고리로 도매가 포함 정밀 탐색 후 진입 후보 확정")
        print(f"{'═'*78}\n")
