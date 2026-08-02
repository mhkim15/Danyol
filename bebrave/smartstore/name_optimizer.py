"""
상품명 최적화 전용 모듈 (2026-07-12 사용자 요청으로 content.py에서 분리).

목표: ANTHROPIC_API_KEY 없이도 도매매 원본 상품명에 흔한 동의어/유의어 중복
(예: "우산 양산 양우산 자동우산 우양산")을 제거하고, 네이버쇼핑 SEO 가이드
(동의어 반복 금지·50자 미만·단어경계 유지)에 맞는 상품명을 만든다.

한계: SYNONYM_GROUPS 사전에 등록된 동의어만 잡는다 — 완벽한 NLP 동의어 인식이 아니라
사전 기반 매칭. 새 카테고리를 다룰 때마다 그룹을 보강해야 함. Claude API 키가 있으면
`content._generate_with_claude()`가 더 정교하게 처리하고, 이 모듈은 마지막 안전장치
(단어경계 절단)로만 관여한다.
"""
import re
from typing import List, Set

MAX_NAME_LEN = 45

# 네이버쇼핑 SEO 가이드가 금지하는 판매조건·홍보문구 — Claude 경로는 프롬프트로 지시하지만
# API 키 없는 폴백 경로(optimize_name)와 태그 생성(content._generate_tags)엔 필터가 없어서
# 그대로 새어나가고 있었음 (2026-08 재점검에서 발견). 어뷰징으로 검색 노출 페널티 리스크.
BANNED_PROMO_WORDS = {
    "무료배송", "배송비무료", "당일발송", "당일출고", "특가", "초특가", "핫딜",
    "인기", "베스트", "1+1", "2+1", "3+1", "세일", "할인", "정품", "최저가",
    "단독", "이벤트", "사은품", "적립금", "쿠폰", "프로모션", "광고", "협찬",
    "재입고", "품절임박", "수량한정",
}


def strip_promo_words(words: List[str]) -> List[str]:
    return [w for w in words if w not in BANNED_PROMO_WORDS]

# 동의어/유의어 그룹 — 같은 그룹 안에서는 최초 등장 단어(보통 keyword) 하나만 채택.
# "자동우산"/"골프우산"처럼 실제 구분 정보가 붙은 복합어는 그룹의 "정확히 동일한 단어"가
# 아니므로 걸러지지 않고 유지됨 (부분일치가 아니라 완전일치로만 판정하는 게 핵심).
SYNONYM_GROUPS: List[Set[str]] = [
    {"우산", "양산", "양우산", "우양산", "우산겸양산"},
    {"가방", "백", "핸드백", "숄더백"},
    {"신발", "슈즈"},
    {"모자", "캡", "햇", "모자캡"},
    {"주걱", "스패츌러", "스패출러"},
    {"수건", "타올", "타월"},
    {"양말", "삭스"},
    {"장갑", "글러브"},
    {"쿠션", "방석"},
    {"거울", "미러"},
    {"빗", "브러쉬", "브러시"},
    {"수납함", "정리함", "정리박스", "수납박스"},
    {"가디건", "니트가디건"},
    {"스카프", "머플러"},
]


def _synonym_key(word: str) -> str:
    """단어가 속한 동의어 그룹의 대표키. 정확일치로만 판정(부분일치 금지)."""
    for group in SYNONYM_GROUPS:
        if word in group:
            return "|".join(sorted(group))
    return word


# 카테고리별 무드/상황 어휘 — 2030 여성 타겟 클릭률을 위해 상품명 맨 끝에 최대 1개만 붙임.
# 매칭 안 되는 카테고리는 억지로 붙이지 않음(기본 폴백 없음). category는 도매매 분류 경로
# 문자열(예: "생활>수납/정리>정리함")이라 키를 부분일치(in)로 찾는다.
MOOD_WORDS = {
    "뷰티소품": "홈셀프케어",
    "네일케어": "셀프네일",
    "헤어스타일링": "홈스타일링",
    "헤어케어": "홈케어",
    "헤어액세서리": "데일리스타일링",
    "수납/정리용품": "원룸수납",
    "발건강용품": "홈케어",
    "좌욕/좌훈용품": "홈셀프케어",
    "여성언더웨어/잠옷": "홈웨어룩",
    "주얼리": "데일리주얼리",
}


def _find_mood_word(category: str) -> str:
    for key, word in MOOD_WORDS.items():
        if key in category:
            return word
    return ""


def optimize_name(keyword: str, raw_title: str, category: str = "", max_len: int = MAX_NAME_LEN) -> str:
    """
    동의어 중복 제거 + 키워드 앞배치 + 무드어휘 1개(선택) + 단어경계 절단.

    예: keyword="우산", raw_title="우산 양산 양우산 자동우산 (인쇄가능) 3단자동우산 우양산 골프우산 ..."
        → "우산 자동우산 3단자동우산 골프우산 ..." (양산/양우산/우양산만 제거, 나머지는 유지)
    """
    raw_title = re.sub(r"\[.*?\]|\(.*?\)", "", raw_title).strip()
    words = strip_promo_words(raw_title.split())

    chosen: List[str] = []
    seen_keys = set()

    if keyword:
        chosen.append(keyword)
        seen_keys.add(_synonym_key(keyword))

    for w in words:
        key = _synonym_key(w)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        chosen.append(w)

    mood_word = _find_mood_word(category) if category else ""
    if mood_word and mood_word not in chosen:
        candidate = " ".join(chosen + [mood_word])
        if len(candidate) <= max_len:
            chosen.append(mood_word)

    return truncate_at_word_boundary(" ".join(chosen), max_len)


def truncate_at_word_boundary(text: str, max_len: int) -> str:
    """문자 단위로 자르면 "골프우산"이 "골프우"처럼 단어 중간에 잘리는 문제를 방지."""
    if len(text) <= max_len:
        return text
    out: List[str] = []
    length = 0
    for w in text.split():
        add_len = len(w) + (1 if out else 0)
        if length + add_len > max_len:
            break
        out.append(w)
        length += add_len
    return " ".join(out) if out else text[:max_len]
