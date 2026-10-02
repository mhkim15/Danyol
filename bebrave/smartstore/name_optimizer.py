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

from ..config import BLOCKED_BRAND_PREFIXES, BLOCKED_MEDICAL_KEYWORDS, EXTERNAL_MALL_NAMES, HYPE_PHRASES

# 네이버쇼핑 SEO 가이드(2026-08) 20쪽 위반 예시("지나치게 긴 상품명")가 20자 안팎을
# 보여준다 — 기존 45자는 넉넉한 편이라 40자로 낮춘다.
MAX_NAME_LEN = 40

# 네이버쇼핑 SEO 가이드가 금지하는 판매조건·홍보문구 — Claude 경로는 프롬프트로 지시하지만
# API 키 없는 폴백 경로(optimize_name)와 태그 생성(content._generate_tags)엔 필터가 없어서
# 그대로 새어나가고 있었음 (2026-08 재점검에서 발견). 어뷰징으로 검색 노출 페널티 리스크.
BANNED_PROMO_WORDS = {
    "무료배송", "배송비무료", "당일발송", "당일출고", "특가", "초특가", "핫딜",
    "인기", "베스트", "1+1", "2+1", "3+1", "세일", "할인", "정품", "최저가",
    "단독", "이벤트", "사은품", "적립금", "쿠폰", "프로모션", "광고", "협찬",
    "재입고", "품절임박", "수량한정",
    # 도매 B2B 문구 — 도매매 원본은 대량 사입/인쇄 주문 고객을 대상으로 쓴 문구라
    # 우리(위탁 소매)와 무관하다. "인쇄가능"이 상품명에 그대로 남으면 실제로 인쇄를
    # 안 해주므로 허위 표시가 된다(2026-09 발견).
    "인쇄가능", "사입가능", "도매가능", "대량구매", "소량가능",
    # 배송·수량 판매조건 — "당일발송"만 있고 "당일배송"이 없어 "10개가격 당일배송"이
    # 상품명에 그대로 남았다(2026-10).
    "당일배송", "빠른배송", "익일배송", "개가격",
}

# 흔한 맞춤법 오류 — 검색량은 있어도 상품명·태그에 쓰면 품질이 떨어져 보이고, 정상 표기와
# 함께 쓰면 같은 말 반복이 된다("목베개 … 배개", 태그 "배게베개", 2026-10).
# 상품명에서는 지우지 않고 바로잡는다 — "발톱깍기"를 지우면 상품 유형이 통째로 사라진다.
# 태그는 오타가 별개 검색어라 바로잡으면 중복이 되므로 그냥 뺀다(content._is_blocked_tag).
TYPO_FIXES = {"배개": "베개", "배게": "베개", "깍이": "깎이", "깍기": "깎이"}


def has_typo(word: str) -> bool:
    return any(t in word for t in TYPO_FIXES)


def fix_typos(word: str) -> str:
    for wrong, right in TYPO_FIXES.items():
        word = word.replace(wrong, right)
    return word

# 도매매 공급사 내부 관리코드 — "RD-10098"(코드+숫자), "-TJ"(하이픈+짧은 알파벳)처럼
# 상품명에 토큰으로 섞여 들어온다. 슬래시 토큰화 수정(2026-09) 전에는 나열형 문자열에
# 묻혀 있어 안 걸렸다.
_SUPPLIER_CODE_RE = re.compile(r"^-?[A-Za-z]{1,4}-\d{2,}$|^-[A-Za-z]{1,4}$")


def _is_supplier_code(word: str) -> bool:
    return bool(_SUPPLIER_CODE_RE.match(word))


def strip_promo_words(words: List[str]) -> List[str]:
    # 완전일치였던 예전 판정은 "무료배송!"·"★특가★"처럼 특수문자가 붙으면 그대로
    # 통과했다(2026-09 발견) — 부분일치로 바꿔 우회를 막는다.
    return [w for w in words if not any(b in w for b in BANNED_PROMO_WORDS)]


def _is_blocked_brand(word: str) -> bool:
    """소싱 단계(discover.py)는 키워드 자체에 타사 브랜드명이 섞이면 후보에서 아예
    거르지만, 도매매 원본 제목에 브랜드어가 섞여 들어오는 건 걸러지지 않고 그대로
    상품명에 남았다(2026-09 발견, 예: "…대코 브라이트…"). 같은 차단 목록을 재사용해
    상품명 정제에도 적용 — 새 목록을 따로 만들지 않는다."""
    w = word.lower()
    return any(b.lower() in w for b in BLOCKED_BRAND_PREFIXES)


# 렌탈/해외/중고 여부는 네이버가 상품 상태 체크박스로 상품명에 자동 노출한다 —
# 여기 또 적으면 중복 표시가 된다(가이드 13쪽 "렌탈/해외/중고 상품 여부는 상품명에
# 기입을 지양해주세요").
RENTAL_STATUS_WORDS = {
    "렌탈", "렌탈상품", "해외구매", "해외직구", "해외배송", "해외상품",
    "중고", "중고상품", "리퍼", "리퍼비시",
}

# 가이드 20쪽 "특수문자" 위반 예시(★▶◀ 등) — 한글/영문/숫자/공백/하이픈만 남긴다.
_SPECIAL_CHAR_RE = re.compile(r"[^0-9A-Za-z가-힣\s\-]")


def _strip_special_chars(text: str) -> str:
    return _SPECIAL_CHAR_RE.sub("", text)


def _is_blocked_word(word: str) -> bool:
    """상품명 단어 하나가 가이드 위반 사유(타사 브랜드·공급사코드·렌탈/해외/중고
    표기·타사 오픈마켓명·과장 표현)에 걸리는지 한 곳에서 판정. optimize_name(폴백
    경로)과 sanitize_ai_name(Claude 경로)이 이 함수 하나를 공유한다 — 예전엔
    폴백 경로에만 필터가 걸려 있었다(2026-09 발견)."""
    if _is_blocked_brand(word) or _is_supplier_code(word):
        return True
    if word in RENTAL_STATUS_WORDS:
        return True
    if any(m in word for m in EXTERNAL_MALL_NAMES):
        return True
    if any(h in word for h in HYPE_PHRASES):
        return True
    # 질환명("내성발톱")은 의약품 오인 표현이라 상품명에 쓰면 안 된다 — 소싱 키워드 단계에서만
    # 막고 있어 도매 원본 제목에 섞인 건 그대로 남았다(2026-10).
    if any(m in word for m in BLOCKED_MEDICAL_KEYWORDS):
        return True
    return False


def _clean_word_list(words: List[str], chosen: List[str], seen_keys: set) -> None:
    """words를 필터링해 chosen에 이어붙인다(in-place) — 동의어 중복·차단어·
    이미 고른 단어에 완전 포함되는 정보 없는 단어를 걸러낸다."""
    for w in words:
        if _is_blocked_word(w):
            continue
        key = _synonym_key(w)
        if key in seen_keys:
            continue
        # 동의어 그룹 밖이라도, 이미 고른 단어에 완전히 포함되는 단어는 정보가 없다
        # (예: "실리콘주걱"을 이미 골랐는데 뒤에 "실리콘"만 또 나오는 경우). "자동우산"이
        # "우산"을 부분 포함하는 것과는 반대 방향 — 짧은 단어가 이미 고른 긴 단어 안에
        # 완전히 들어갈 때만 걸러야, "자동우산"·"골프우산"처럼 실제 구분 정보가 붙은
        # 복합어는 그대로 유지된다(2026-09).
        if any(w in c for c in chosen):
            continue
        # 반대 방향 중복 — 이미 고른 3글자 이상 단어를 통째로 품은 단어는 같은 키워드를 한 번 더
        # 쓰는 셈이다("며느리발톱 … 며느리발톱제거", "쿠션퍼프 … 왕쿠션퍼프", 2026-10). 2글자
        # 단어("우산")는 예외 — "자동우산"·"골프우산"처럼 구분 정보가 붙은 복합어를 살린다.
        # 맨 앞(소싱 키워드)을 품은 단어는 버리고, 그 외에는 더 구체적인 쪽으로 바꿔 끼운다
        # ("자동우산" → "3단자동우산" — 정보는 살리고 반복만 없앤다).
        inner = next((i for i, c in enumerate(chosen) if len(c) >= 3 and c in w), None)
        if inner is not None:
            if inner > 0:
                chosen[inner] = w
            continue
        # 같은 꼬리(마지막 3글자)를 가진 복합어는 2개까지만 — "여행용목베개 기내용목베개
        # 캠핑목베개"처럼 쓰임새만 바꿔 나열하는 건 동의어 나열과 같다(2026-10).
        if len(w) >= 4 and sum(1 for c in chosen if len(c) >= 4 and c[-3:] == w[-3:]) >= 2:
            continue
        seen_keys.add(key)
        chosen.append(w)

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
# 상품과 어울릴 때만 붙도록 좁혔다(2026-10) — "네일케어" 전체에 "셀프네일"을 붙여 손톱깎이가
# "…셀프네일"이 됐고, "뷰티소품"의 "홈셀프케어"는 퍼프에 붙어 뜻이 안 통했다.
MOOD_WORDS = {
    "네일아트": "셀프네일",
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
    # 공백뿐 아니라 슬래시도 단어 구분자로 처리한다 — 안 그러면
    # "손톱깍이/손톱깍기/손톱깍이세트/…/인쇄가능"처럼 슬래시로 나열된 통짜 토큰
    # 하나가 공백 기준 split()을 그대로 통과해 중복 제거·홍보어·브랜드 필터를
    # 전부 우회했다(2026-09 발견). 특수문자 제거는 분리 "후" 단어별로 해야 한다 —
    # 슬래시째로 지우면 나열된 단어들이 한 토큰으로 도로 뭉쳐버린다.
    words = [fix_typos(_strip_special_chars(w)) for w in re.split(r"[\s/]+", raw_title) if w]
    words = strip_promo_words([w for w in words if w])

    chosen: List[str] = []
    seen_keys = set()

    keyword = fix_typos(keyword or "")
    half = len(keyword) // 2
    if half and keyword[:half] == keyword[half:]:  # "배게베개" → "베개베개" → "베개"
        keyword = keyword[:half]
    if keyword:
        chosen.append(keyword)
        seen_keys.add(_synonym_key(keyword))

    _clean_word_list(words, chosen, seen_keys)

    mood_word = _find_mood_word(category) if category else ""
    if mood_word and mood_word not in chosen:
        candidate = " ".join(chosen + [mood_word])
        if len(candidate) <= max_len:
            chosen.append(mood_word)

    return truncate_at_word_boundary(" ".join(chosen), max_len)


def sanitize_ai_name(text: str, max_len: int = MAX_NAME_LEN) -> str:
    """Claude가 생성한 상품명에 사후 필터를 적용한다. 프롬프트 지시만으로는
    금지 문구·특수문자·타사몰명·렌탈/중고 표기가 새어나갈 수 있다 — 지금까지
    이 필터는 API 키 없는 폴백 경로(optimize_name)에만 걸려 있었다(2026-09 발견).
    AI가 이미 단어 순서를 잡아준 뒤라 keyword를 앞에 강제로 끼워넣지 않고, 같은
    차단·중복 제거 로직만 통과시킨다."""
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text or "").strip()
    words = [fix_typos(_strip_special_chars(w)) for w in re.split(r"[\s/]+", text) if w]
    words = strip_promo_words([w for w in words if w])

    chosen: List[str] = []
    seen_keys: set = set()
    _clean_word_list(words, chosen, seen_keys)

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
