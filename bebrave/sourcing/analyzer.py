import csv
import json
from pathlib import Path
from typing import Optional, Union, Tuple, List

from ..config import (
    GOLDEN_RATIO,
    MIN_SEARCH,
    MAX_SEARCH,
    MAX_COMPETITION,
    BLOCKED_CATEGORIES,
    TARGET_CATEGORIES,
)
from .models import ProductCandidate

# 멀티팩터 스코어 가중치
_CATEGORY_SCORE = {
    "주방용품": 20, "욕실용품": 15, "생활용품": 15, "정리수납": 15,
}


def analyze(
    keyword: str,
    monthly_search: int,
    product_count: int,
    category: str,
    is_seasonal: bool = False,
    notes: str = "",
    est_sale_price: int = 0,
    est_cost_price: int = 0,
) -> Tuple[ProductCandidate, List[str]]:
    """
    상품 후보를 분석하고 통과/경고 사유를 반환.
    Returns: (ProductCandidate, warnings)
    """
    candidate = ProductCandidate(
        keyword=keyword,
        monthly_search=monthly_search,
        product_count=product_count,
        category=category,
        is_seasonal=is_seasonal,
        notes=notes,
        est_sale_price=est_sale_price,
        est_cost_price=est_cost_price,
    )
    candidate.score = _score(candidate)
    warnings = _check(candidate)
    return candidate, warnings


def _score(c: ProductCandidate) -> int:
    """
    멀티팩터 소싱 점수 계산 (0~100점).
    - 골든레이시오 점수: 40점 (≥5.0이면 만점)
    - 검색수 구간 점수: 30점 (3,000~7,000이 최적)
    - 카테고리 적합성: 20점
    - 계절성 감점: -10점
    """
    score = 0

    # 골든레이시오 점수 (40점)
    ratio = c.golden_ratio
    if ratio == float("inf"):
        ratio_score = 40
    elif ratio >= 5.0:
        ratio_score = 40
    elif ratio >= 3.0:
        ratio_score = 28
    elif ratio >= 2.0:
        ratio_score = 16
    else:
        ratio_score = 0
    score += ratio_score

    # 검색수 구간 점수 (30점) — 3,000~7,000 최적
    s = c.monthly_search
    if 3_000 <= s <= 7_000:
        search_score = 30
    elif 2_000 <= s < 3_000 or 7_000 < s <= 9_000:
        search_score = 20
    elif MIN_SEARCH <= s < 2_000 or 9_000 < s <= MAX_SEARCH:
        search_score = 10
    else:
        search_score = 0
    score += search_score

    # 카테고리 적합성 (20점)
    score += _CATEGORY_SCORE.get(c.category, 5)

    # 계절성 감점 (-10점)
    if c.is_seasonal:
        score -= 10

    return max(0, min(100, score))


def _check(c: ProductCandidate) -> list[str]:
    warnings = []
    if c.golden_ratio < GOLDEN_RATIO:
        warnings.append(
            f"골든레이시오 미달: {c.golden_ratio} (기준 {GOLDEN_RATIO} 이상 필요)"
        )
    if c.monthly_search < MIN_SEARCH:
        warnings.append(f"검색수 부족: {c.monthly_search:,}건 (최소 {MIN_SEARCH:,}건)")
    if c.monthly_search > MAX_SEARCH:
        warnings.append(
            f"검색수 초과 (경쟁 과열 가능): {c.monthly_search:,}건 (상한 {MAX_SEARCH:,}건)"
        )
    if c.product_count > MAX_COMPETITION:
        warnings.append(
            f"경쟁 상품 과다: {c.product_count:,}개 (상한 {MAX_COMPETITION:,}개)"
        )
    if c.category in BLOCKED_CATEGORIES:
        warnings.append(f"위험 카테고리: '{c.category}' (KC인증·반품 위험)")
    if c.is_seasonal:
        warnings.append("계절성 상품 — 비수기 재고 리스크 주의")
    return warnings


def filter_candidates(candidates: list[ProductCandidate]) -> list[ProductCandidate]:
    """골든레이시오·검색수·경쟁도 기준을 모두 통과한 후보만 반환 (비율 내림차순 정렬)."""
    passed = [
        c for c in candidates
        if c.golden_ratio >= GOLDEN_RATIO
        and MIN_SEARCH <= c.monthly_search <= MAX_SEARCH
        and c.product_count <= MAX_COMPETITION
        and c.category not in BLOCKED_CATEGORIES
    ]
    return sorted(passed, key=lambda c: c.golden_ratio, reverse=True)


def import_from_csv(path: Union[str, Path]) -> List[Tuple[ProductCandidate, List[str]]]:
    """
    CSV 파일에서 키워드 목록을 일괄 분석.

    CSV 컬럼 (헤더 필수):
      keyword, monthly_search, product_count, category,
      is_seasonal (true/false, 선택), notes (선택),
      est_sale_price (선택), est_cost_price (선택)
    """
    results = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keyword = row["keyword"].strip()
            if not keyword:
                continue
            candidate, warnings = analyze(
                keyword=keyword,
                monthly_search=int(row["monthly_search"]),
                product_count=int(row["product_count"]),
                category=row["category"].strip(),
                is_seasonal=row.get("is_seasonal", "false").strip().lower() in ("true", "1", "yes"),
                notes=row.get("notes", "").strip(),
                est_sale_price=int(row.get("est_sale_price") or 0),
                est_cost_price=int(row.get("est_cost_price") or 0),
            )
            results.append((candidate, warnings))
    return results


def check_margin(candidate: ProductCandidate) -> Optional[object]:
    """소싱 후보의 예상 마진율 계산. est_sale_price/est_cost_price 미입력 시 None 반환."""
    if not candidate.est_sale_price or not candidate.est_cost_price:
        return None
    from ..margin.calculator import calculate
    return calculate(candidate.est_sale_price, candidate.est_cost_price)


def load_from_json(path: Union[str, Path]) -> list:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return [ProductCandidate.from_dict(d) for d in data]


def save_to_json(candidates: list, path: Union[str, Path]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in candidates], f, ensure_ascii=False, indent=2)
