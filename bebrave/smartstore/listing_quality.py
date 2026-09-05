"""
등록 품질 채점 — 무판매 상품 진단.

노출·클릭 API가 스마트스토어엔 없어(bebrave/report/performance.py 참고)
"왜 안 팔리나"를 직접 볼 수 없다. 대신 통제 가능한 변수를 점수화해서
"이것부터 고쳐라"를 제시한다: 상품명 길이·키워드 선두배치·금지문구,
절대이익 수준(로컬 데이터만으로 채점 가능) + 이미지 수·상세설명 분량·태그 수
(실시간 조회가 있을 때만 채점 — register.py의 fetch_registered_product() 응답 필요).

기존 규칙을 그대로 재사용한다 — 새 기준을 만들지 않는다:
  name_optimizer.MAX_NAME_LEN, name_optimizer.BANNED_PROMO_WORDS, config.MIN_ABS_PROFIT
"""
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import MIN_ABS_PROFIT
from .name_optimizer import BANNED_PROMO_WORDS

MIN_IMAGE_COUNT = 3
MIN_DETAIL_LENGTH = 500
MIN_TAG_COUNT = 5
MIN_NAME_LENGTH = 15


# 카테고리별 "어떻게 고치나" — 점수·사유만 나열하면 뭘 해야 할지 알 수 없다는
# 지적으로 추가(2026-09). 화면의 실제 편집 지점을 그대로 가리킨다.
_GUIDE = {
    "상품명": "위 상품명 칸에서 키워드를 맨 앞에 두고 금지 문구를 빼세요.",
    "마진": "판매가를 올리거나 원가가 더 낮은 옵션·공급처로 바꾸는 걸 검토하세요.",
    "이미지": "아래에서 대표·추가 이미지를 늘리세요.",
    "상세설명": "아래 상세설명 편집기에서 내용을 보강하세요.",
    "태그": "아래 검색어 태그 칸에서 태그를 추가하세요(최대 10개).",
}


@dataclass
class QualityIssue:
    item: str
    detail: str
    penalty: int

    @property
    def guide(self) -> str:
        return _GUIDE.get(self.item, "")


@dataclass
class QualityScore:
    score: int
    issues: List[QualityIssue] = field(default_factory=list)
    checked_live: bool = False  # True면 이미지·태그·상세설명까지 채점(실시간 조회 필요)

    def top_issue(self) -> str:
        return self.issues[0].detail if self.issues else ""

    @property
    def tier(self) -> str:
        """점수만 던지면 "75점이 좋은 건지 나쁜 건지" 알 수 없다는 지적으로 추가(2026-09).
        경계값은 이 파일의 감점 단위(10~20점씩)를 기준으로 잡았다 — 이슈 0~1개는 양호,
        2~3개는 보완 권장, 그 이상은 보완 필요로 갈리는 지점."""
        if self.score >= 85:
            return "양호"
        if self.score >= 60:
            return "보완 권장"
        return "보완 필요"


def score_listing(product: dict, live_detail: Optional[dict] = None) -> QualityScore:
    """product: registered_products.json 레코드 1건.
    live_detail: register.fetch_registered_product() 응답(있으면 이미지/태그/상세설명도 채점)."""
    issues: List[QualityIssue] = []
    score = 100

    name = product.get("name", "")
    keyword = product.get("keyword", "")
    if keyword and not name.startswith(keyword):
        issues.append(QualityIssue("상품명", f"키워드 '{keyword}'가 맨 앞에 없음 — 검색 노출에 불리", 15))
    if len(name) < MIN_NAME_LENGTH:
        issues.append(QualityIssue("상품명", f"{len(name)}자로 짧음(권장 {MIN_NAME_LENGTH}자+) — 정보량 부족", 10))
    promo_hit = [w for w in BANNED_PROMO_WORDS if w in name]
    if promo_hit:
        issues.append(QualityIssue("상품명", f"금지 홍보문구 포함: {', '.join(promo_hit)} — 노출 페널티 위험", 20))

    sale_price = product.get("sale_price", 0) or 0
    margin_rate = product.get("margin_rate", 0) or 0
    net_profit = round(sale_price * margin_rate)
    if net_profit < MIN_ABS_PROFIT:
        issues.append(QualityIssue("마진", f"절대이익 {net_profit:,}원 — 기준({MIN_ABS_PROFIT:,}원) 미달", 15))

    checked_live = live_detail is not None
    if live_detail is not None:
        origin = live_detail.get("originProduct", {}) or {}
        images = origin.get("images", {}) or {}
        image_count = (1 if images.get("representativeImage") else 0) + len(images.get("optionalImages") or [])
        if image_count < MIN_IMAGE_COUNT:
            issues.append(QualityIssue("이미지", f"{image_count}장 — 최소 {MIN_IMAGE_COUNT}장 권장", 15))

        detail_len = len(origin.get("detailContent", "") or "")
        if detail_len < MIN_DETAIL_LENGTH:
            issues.append(QualityIssue("상세설명", f"{detail_len}자 — 정보 부족", 15))

        tags = ((origin.get("detailAttribute", {}) or {}).get("seoInfo", {}) or {}).get("sellerTags") or []
        if len(tags) < MIN_TAG_COUNT:
            issues.append(QualityIssue("태그", f"{len(tags)}개 — 검색 노출 기회 부족(권장 {MIN_TAG_COUNT}개+)", 10))

    total_penalty = sum(i.penalty for i in issues)
    issues.sort(key=lambda i: i.penalty, reverse=True)
    return QualityScore(score=max(0, 100 - total_penalty), issues=issues, checked_live=checked_live)


def _demo() -> None:
    """실행 가능한 자체 점검 — 로컬/실시간 채점 둘 다 검증 (네트워크 호출 없음)."""
    good = {"name": "실리콘주걱 대코 브라이트 미니볶음주걱", "keyword": "실리콘주걱",
            "sale_price": 20000, "margin_rate": 0.3}
    r = score_listing(good)
    assert r.score == 100 and not r.checked_live, "정상 상품인데 감점됨"

    bad = {"name": "최저가", "keyword": "실리콘주걱", "sale_price": 1000, "margin_rate": 0.01}
    r = score_listing(bad)
    assert r.score < 100 and r.checked_live is False
    assert any("최저가" in i.detail or "홍보문구" in i.item for i in r.issues) or \
           any(i.item == "상품명" for i in r.issues), "짧고 금지문구 포함된 이름을 못 잡음"

    live = {"originProduct": {"images": {"representativeImage": {"url": "x"}}, "detailContent": "짧음",
                               "detailAttribute": {"seoInfo": {"sellerTags": [{"text": "a"}]}}}}
    r2 = score_listing(good, live_detail=live)
    assert r2.checked_live and r2.score < 100, "이미지 1장·상세 짧음·태그 1개인데 실시간 채점이 안 잡음"

    assert score_listing(good).tier == "양호", "감점 없는 상품이 양호로 안 뜸"
    assert r2.tier in ("보완 권장", "보완 필요"), "감점 있는 상품 등급 판정이 이상함"
    for issue in r2.issues:
        assert issue.guide, f"'{issue.item}' 항목에 보완 가이드가 없음"

    print("listing_quality self-check OK")


if __name__ == "__main__":
    _demo()
