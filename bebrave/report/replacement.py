"""
교체 후보 추천 — 무판매로 판매중지할 상품 자리에 넣을 대체 상품을 발굴 후보
(sourcing_log.json)에서 찾는다. 이미 등록된 상품(도매매 상품번호 겹침)은 제외하고,
진입 권장 점수를 넘는 것 중 같은 키워드 계열을 우선, 그다음 점수순으로 제안한다.
"""
from typing import List, Optional

_DEFAULT_MIN_SCORE = 55  # discover.py의 "진입 권장" 기준과 통일


def suggest_replacements(
    keyword: str,
    candidates: list,
    registered: list,
    limit: int = 3,
    min_score: int = _DEFAULT_MIN_SCORE,
) -> List[dict]:
    registered_goods = {p.get("domemae_goods_no") for p in registered if p.get("domemae_goods_no")}
    pool = [
        c for c in candidates
        if c.get("score", 0) >= min_score
        and c.get("supply_goods_no") not in registered_goods
    ]

    def _rank(c: dict):
        related = bool(keyword) and (keyword in c.get("keyword", "") or c.get("keyword", "") in keyword)
        return (0 if related else 1, -c.get("score", 0))

    pool.sort(key=_rank)
    return pool[:limit]


def _demo() -> None:
    """실행 가능한 자체 점검 — 등록상품 제외, 키워드 우선순위만 검증 (파일 IO 없음)."""
    candidates = [
        {"keyword": "실리콘주걱", "score": 80, "supply_goods_no": "111"},   # 이미 등록됨 — 제외돼야 함
        {"keyword": "실리콘주걱 미니형", "score": 70, "supply_goods_no": "222"},  # 관련 키워드(부분일치)
        {"keyword": "완전다른상품", "score": 90, "supply_goods_no": "333"},  # 점수는 높지만 무관
        {"keyword": "우산", "score": 40, "supply_goods_no": "444"},         # 기준 미달
    ]
    registered = [{"domemae_goods_no": "111"}]

    result = suggest_replacements("실리콘주걱", candidates, registered, limit=3)
    assert all(c["supply_goods_no"] != "111" for c in result), "이미 등록된 상품이 섞임"
    assert result[0]["keyword"] == "실리콘주걱 미니형", "관련 키워드가 우선순위에서 밀림"
    assert len(result) == 2, "기준 미달 후보가 섞이거나 개수가 안 맞음"

    print("replacement self-check OK")


if __name__ == "__main__":
    _demo()
