"""
Claude API를 활용한 상품 콘텐츠 자동 생성.

- 상품명: 네이버쇼핑 SEO 가이드 기준 (동의어/유의어 반복 금지, 50자 미만, 단어 경계 유지)
- 상세설명: 도매매 원본 상세설명(desc.contents) + 이미지 + 핵심 정보
- 검색어 태그: 노출에 유리한 5개 내외 태그 생성

가이드 출처 (2026-07-12 조사, 2026-08 공식 SEO 가이드로 재확인): 상품명은 동의어·유의어
중복·판매조건·홍보문구·판매처명(스토어명) 포함 금지. 반대로 브랜드/카테고리(상품유형)는
필수 기입 — "카테고리명 포함 금지"로 잘못 적혀 있던 지시(2026-09 정정: 가이드 12쪽은
브랜드+상품유형+핵심속성 조합을 요구하지, 카테고리명 자체를 금지하지 않는다. 태그는
반대로 카테고리명이 금지 — content._is_blocked_tag 참고). 검색어 태그는 상품과 무관한
걸 억지로 채우는 것보다 5~7개 정도가 유리.

환경변수:
  ANTHROPIC_API_KEY
"""
import os
import re
from typing import TYPE_CHECKING, List, Optional

from .name_optimizer import BANNED_PROMO_WORDS, MAX_NAME_LEN as _MAX_NAME_LEN
from .name_optimizer import _find_mood_word, _is_blocked_brand, has_typo, optimize_name, sanitize_ai_name
from ..sourcing.keyword_tool import fetch_related_keywords

if TYPE_CHECKING:
    from ..sourcing.domemae import DomemaeProduct


def _tokens(text: str) -> set:
    return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text or ""))


def related_demand_keywords(keyword: str, product: "DomemaeProduct", limit: int = 30) -> List["KeywordData"]:
    """
    네이버 검색광고 API로 실제 월검색수가 있는 관련 키워드를 조회.
    2026-08 시뮬레이션(50건 실측)으로 확인: 원본 제목/카테고리와 단어가 겹치는 것만
    걸러서 검색량 순으로 골라야 무관한 대형 키워드(예: "마사지")가 안 섞임.
    API 실패/키 미설정이면 조용히 빈 리스트 — 호출부가 다른 태그로 채운다.

    태그 채택(_demand_tags)과 미리보기 화면의 "추천 키워드" 목록(2026-09 추가)이 같은
    관련성 판정 로직을 쓴다 — 예전엔 상위 3개만 쓰고 나머지 27개와 검색량을 그냥 버렸다.
    """
    try:
        related = fetch_related_keywords(keyword, limit=30)
    except Exception:
        return []

    # 부분일치(substring) 기준 — 관련 키워드가 붙여쓰기 복합어("두피브러쉬")로 오는 경우가
    # 많아서, 원본 제목의 개별 토큰("두피")과 정확히 같은 통짜 토큰이어야 한다는 조건(교집합)은
    # 대부분 걸러버림. base_tokens 각각이 관련 키워드 문자열 "안에" 들어있는지로 완화
    # (2026-08 50건 실측에서 발견 — 이 조건 때문에 매칭률이 실제보다 훨씬 낮게 나왔었음).
    # 3글자 이상 토큰만 근거로 쓴다 — "네일"·"베개" 같은 2글자 토큰은 거의 모든 연관어에 들어가
    # 일자손톱깎이에 네일드릴·네일오일펜이, 방수베개커버에 긴베개·쿨링베개가 붙었다(2026-10 실측).
    # 카테고리 이름도 근거에서 뺐다 — "네일케어도구" 때문에 "네일케어비트"가 붙었다(2026-10).
    base_tokens = {t for t in _tokens(product.name) if len(t) >= 3}
    relevant = [
        r for r in related
        if r.monthly_total > 0
        and r.keyword != keyword
        and r.keyword not in BANNED_PROMO_WORDS
        # 상품명에 없는 "자동"이 붙은 연관어는 다른 상품이다(수동 손톱깎이에 "자동손톱깎이")
        and ("자동" not in r.keyword or "자동" in product.name)
        and (
            (len(keyword) >= 3 and keyword in r.keyword)  # 2글자 키워드("베개")는 거의 모든 연관어에 들어간다
            or r.keyword in product.name
            or any(bt in r.keyword for bt in base_tokens)
        )
    ]
    relevant.sort(key=lambda r: r.monthly_total, reverse=True)
    return relevant[:limit]


def _demand_tags(keyword: str, product: "DomemaeProduct", limit: int = 3) -> List[str]:
    return [r.keyword for r in related_demand_keywords(keyword, product, limit=limit)]


def generate_product_content(
    keyword: str,
    product: "DomemaeProduct",
    sale_price: int,
    category_name: str = "",
) -> dict:
    """
    AI 기반 상품명 + 상세설명 + 검색어 태그 생성.

    category_name: 확정된 스마트스토어 리프 카테고리 경로(예: "생활>침구>이불커버",
    category.describe_category() 결과) — 태그가 카테고리명을 그대로 달지 않도록 걸러내는
    데만 쓴다(가이드 11쪽 "카테고리, 브랜드명, 판매처명은 태그로 사용 불가"). 없으면
    카테고리 기반 태그 차단만 건너뛴다.

    Returns:
        {"name": str, "detail_content": str, "tags": List[str]}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if api_key:
        return _generate_with_claude(keyword, product, sale_price, api_key, category_name)
    else:
        # Claude API 키 없을 때 기본 템플릿 사용
        return _generate_fallback(keyword, product, category_name)


def _generate_with_claude(
    keyword: str,
    product: "DomemaeProduct",
    sale_price: int,
    api_key: str,
    category_name: str = "",
) -> dict:
    try:
        import anthropic
    except ImportError:
        return _generate_fallback(keyword, product, category_name)

    client = anthropic.Anthropic(api_key=api_key)

    mood_word = _find_mood_word(product.category)
    mood_hint = (
        f'- 무드어휘 후보: "{mood_word}" — 문맥상 자연스러우면 상품명 끝에 최대 1개만 활용, 어색하면 넣지 말 것'
        if mood_word
        else "- 무드어휘를 억지로 지어내지 말 것"
    )

    name_prompt = f"""네이버 스마트스토어 상품명을 작성해줘.

소싱 키워드: {keyword}
도매매 원본 상품명: {product.name}
카테고리: {product.category}
판매가: {sale_price:,}원

네이버쇼핑 SEO 가이드 규칙 (반드시 준수):
- 40자 이내
- 브랜드(있는 경우) + 핵심 키워드(상품유형) + 핵심 속성(색상/소재/수량 등) + 시즌/사이즈 순서로 구성 — 핵심 키워드를 맨 앞에
- 동의어·유의어를 나열하지 말 것 (예: "우산 양산 양우산 자동우산"처럼 같은 뜻 반복 금지 — 어뷰징으로 간주되어 검색 노출에 불리함)
- 배송·할인·판매조건·홍보 문구, 스토어명(판매처명), 렌탈/해외/중고 여부 포함 금지
{mood_hint}
- 특수문자(★▶◀! 등) 쓰지 말 것 — 한글/영문/숫자/공백/하이픈만 사용
- 상품명만 출력 (설명 없이)"""

    name_msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": name_prompt}],
    )
    # 프롬프트 지시만으론 금지 문구·특수문자·타사몰명이 새어나갈 수 있어(2026-09 발견,
    # 사후 필터가 폴백 경로에만 걸려 있었음) 폴백과 같은 필터를 여기도 통과시킨다.
    optimized_name = sanitize_ai_name(name_msg.content[0].text.strip())

    detail_content = _build_detail_html(product, keyword)
    tags = _generate_tags(keyword, product, category_name)

    return {"name": optimized_name, "detail_content": detail_content, "tags": tags}


def _generate_fallback(
    keyword: str,
    product: "DomemaeProduct",
    category_name: str = "",
) -> dict:
    """Claude API 없을 때 기본 상품명 + 상세설명 생성."""
    name = optimize_name(keyword, product.name, category=product.category)
    detail_content = _build_detail_html(product, keyword)
    tags = _generate_tags(keyword, product, category_name)
    return {"name": name, "detail_content": detail_content, "tags": tags}


# 네이버 커머스API sellerTags는 실제로는 10개까지 반영된다 — 예전엔 5개로 캡을 걸어
# 나머지 절반의 SEO 여지를 그냥 버리고 있었다(2026-09 확인, 공식 가이드 기준).
MAX_TAGS = 10


def _category_segments(category_name: str) -> set:
    """"생활>침구>이불커버" → {"생활","침구","이불커버"}. 태그 차단 전용 —
    가이드 11쪽 "카테고리 필드에 입력"(=태그로는 금지) 예시를 그대로 따른다."""
    return {seg.strip() for seg in re.split(r"[>／/]", category_name or "") if seg.strip()}


def _is_blocked_tag(t: str, category_segments: Optional[set] = None) -> bool:
    """차단 목록은 지금까지 소싱 후보 선별(discover.py)에만 적용되고 태그 생성
    경로는 홍보어 완전일치 하나뿐이었다 — 질환명·타사 브랜드가 태그로 그대로
    나갔다(2026-09 실증: "손톱영양제" 후보가 "손톱무좀"·"손톱조갑박리증" 태그를
    달고 있었음). 태그 생성에도 같은 차단 목록을 재사용한다."""
    from ..config import (BLOCKED_ELECTRIC_KEYWORDS, BLOCKED_INFO_INTENT_SUFFIXES,
                          BLOCKED_KIDS_KEYWORDS, BLOCKED_MEDICAL_KEYWORDS)

    if any(w in t for w in BANNED_PROMO_WORDS):  # 완전일치 → 부분일치(★특가★ 등 우회 방지)
        return True
    if any(w in t for w in BLOCKED_MEDICAL_KEYWORDS):
        return True
    # 아동 연상 단어("어린이베개")는 아동용품(KC 대상)으로 오인시키고, 전기 단어는 상품이
    # 전동인 것처럼 보이게 한다. 한 글자 태그("발")와 흔한 오타("배게베개")도 뺀다(2026-10).
    if any(w in t for w in BLOCKED_KIDS_KEYWORDS + BLOCKED_ELECTRIC_KEYWORDS):
        return True
    if len(t) < 2 or has_typo(t):
        return True
    if _is_blocked_brand(t):
        return True
    if any(m in t for m in _EXTERNAL_MALL_NAMES):  # 판매처명(가이드 11쪽 "판매처명 태그 불가")
        return True
    if any(t.endswith(s) for s in BLOCKED_INFO_INTENT_SUFFIXES):
        return True
    if category_segments and t in category_segments:  # 카테고리명 자체(가이드 11쪽)
        return True
    return False


def _generate_tags(keyword: str, product: "DomemaeProduct", category_name: str = "") -> List[str]:
    """
    검색어 태그 후보 생성 (code 없이 text만 등록 — 네이버 공식 가이드상 code 생략 가능).
    우선순위: ① 소싱 키워드 자체 ② 실제 월검색수가 있는 관련 키워드(수요기반, 최대 8개).
    상품과 무관한 단어는 억지로 안 채운다 — 원본 제목 토큰으로 남는 자리를 채우던
    마지막 단계는 제거했다(2026-09): 카테고리명·브랜드어가 태그로 새는 주 경로였고,
    가이드 10쪽도 "억지로 채우면 적합도에 악영향"이라고 명시한다.

    도매매 카테고리명을 그대로 태그로 넣던 코드는 제거했다 — "수납/정리"처럼
    슬래시 섞인 카테고리 원문이 그대로 나가고 있었다(2026-09).
    """
    segments = _category_segments(category_name)
    seen = set()
    tags = []

    def _add(t: str, cap: int) -> bool:
        t = t.strip()
        if t and t not in seen and not _is_blocked_tag(t, segments):
            seen.add(t)
            tags.append(t)
        return len(tags) >= cap

    if _add(keyword, MAX_TAGS):
        pass

    for t in _demand_tags(keyword, product, limit=MAX_TAGS - 1):
        if _add(t, MAX_TAGS):
            break

    return tags[:MAX_TAGS]


# 도매매 원본 HTML/설명에 섞여 들어오는 제재 리스크 문구 — 네이버 약관상 외부몰
# 링크·직거래 유도 연락처는 즉시 판매정지 사유다(2026-09 적대적 입력 재현으로 확인:
# 외부 스마트스토어 링크·쿠팡 안내·카카오톡 직거래 유도·공급사 전화번호·"국내 1위"·
# "아토피 개선"이 전부 그대로 통과하고 있었다).
# config.py로 이동(2026-09) — name_optimizer의 상품명/태그 게이트도 같은 목록을 쓴다.
from ..config import EXTERNAL_MALL_NAMES as _EXTERNAL_MALL_NAMES, HYPE_PHRASES as _HYPE_PHRASES
# BLOCKED_MEDICAL_KEYWORDS(config.py)는 소싱 키워드 단계의 질환명 차단 목록이라
# "아토피"처럼 상세설명에 흔히 섞이는 의약품 오인 효능 표현까지는 안 담고 있다 —
# 상세페이지 정제 전용으로 별도 보강.
_MEDICAL_OVERCLAIM_PHRASES = ["아토피", "치료효과", "치료 효과", "완치"]
_CONTACT_PATTERNS = [
    re.compile(r"01[016789]-?\d{3,4}-?\d{4}"),               # 휴대폰
    re.compile(r"0\d{1,2}-\d{3,4}-\d{4}"),                    # 일반전화
    re.compile(r"카\s*카\s*오\s*톡?\s*(아이디|id)?\s*[:：]?\s*[A-Za-z0-9_.]{2,}", re.I),
    re.compile(r"카톡\s*(아이디|id)?\s*[:：]?\s*[A-Za-z0-9_.]{2,}", re.I),
]


def sanitize_detail_html(html: str) -> str:
    """도매매 원본 HTML/텍스트에서 외부몰 링크·직거래 유도 연락처·과장/의약품 오인
    표현을 제거한다. <a> 태그는 마크업만 걷어내 링크 기능을 없애고(글 자체는 남김),
    그 외 항목은 문구를 통째로 지운다 — 문장 경계를 안전하게 못 잡는 원본 HTML
    구조상, 남기는 쪽보다 지우는 쪽이 안전하다."""
    from ..config import BLOCKED_MEDICAL_KEYWORDS

    text = html or ""
    text = re.sub(r"</?a\b[^>]*>", "", text, flags=re.I)  # 외부 링크 — 마크업만 제거
    for pattern in _CONTACT_PATTERNS:
        text = pattern.sub("", text)
    for phrase in _EXTERNAL_MALL_NAMES + _HYPE_PHRASES + _MEDICAL_OVERCLAIM_PHRASES + list(BLOCKED_MEDICAL_KEYWORDS):
        text = text.replace(phrase, "")
    return text


def _extract_points(description: str, limit: int = 3, min_len: int = 6, max_len: int = 60) -> List[str]:
    """도매매 원본 설명에서 짧고 실질적인 문장/줄만 골라 상품 포인트로 쓴다.

    지어내지 않는다 — 원본에 없는 장점을 만들어 붙이면 허위·과장 표시가 된다(이
    코드베이스가 원산지·고시 항목에서 이미 지키는 원칙과 동일, 2026-09 적용).
    너무 짧은 줄(메뉴/구분선 잔재)과 너무 긴 줄(문단 전체)은 포인트로 부적절해 제외.

    sanitize_detail_html()을 먼저 태운다 — 안 그러면 홍보/연락처 문구가 길이 조건만
    맞으면 그대로 "상품 포인트"로 승격돼 상단에 노출된다(2026-09 발견: POINT 1~3이
    전부 홍보 문구·연락처로 채워지는 사고).
    """
    text = re.sub(r"<[^>]+>", "\n", sanitize_detail_html(description or ""))
    lines = re.split(r"[\n\r]+|(?<=[.!?다요])\s{2,}", text)
    points = []
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip(" -•·*").strip()
        if min_len <= len(line) <= max_len and line not in points:
            points.append(line)
        if len(points) >= limit:
            break
    return points


def _build_detail_html(product: "DomemaeProduct", keyword: str) -> str:
    """도매매 이미지 + 실제 상세설명(desc.contents) + 판매 정책 안내로 상세설명 HTML 구성.

    공급사명·도매 재고·판매가는 절대 본문에 넣지 않는다 — 예전엔 여기 그대로
    찍혀서 공급사 ID(예: seoul7rsoe)와 도매매 재고(수백만 단위)가 구매자 화면에
    노출되고 있었다. 위탁판매임을 광고하고 소싱처를 경쟁 셀러에게 공개하는
    꼴이었다(2026-09 발견). 판매가는 스마트스토어 판매가 필드가 이미 보여주므로
    본문에 중복 기재하면 가격 변경 시 불일치만 생긴다.

    배송/교환·반품/A/S 안내는 실제 등록 페이로드(register.py의 deliveryInfo·
    afterServiceInfo)와 반드시 같은 값을 쓴다 — 이전에는 도매매 공급사의 출고
    배송비(product.shipping_fee, 우리가 부담하는 매입 배송비)를 구매자 화면에
    "배송비"라고 표시하고 있어서 실제 구매자가 내는 배송비(config.SHIPPING_FEE)와
    다른 숫자를 보여주는 오류가 있었다(2026-09 발견).

    본문 맨 위에 상품명을 다시 찍지 않는다 — 도매 원본 상품명이 그대로 들어가
    "[ABC0671] … 10개가격 당일배송"처럼 공급사 코드·금지 문구가 노출됐다(2026-10).
    상품명은 네이버가 상세페이지 위에 이미 보여준다.
    """
    from ..config import SHIPPING_FEE, FREE_SHIPPING_THRESHOLD, RETURN_DELIVERY_FEE, EXCHANGE_DELIVERY_FEE
    from .notice import CS_PHONE_NUMBER

    img_tags = ""
    for img_url in product.images:
        img_tags += f'<img src="{img_url}" style="width:100%;max-width:860px;" />\n'

    # 원본 설명에 실제로 있는 내용만 포인트로 뽑는다 — 없으면 이 블록 자체를 생략.
    points = _extract_points(product.description)
    points_html = ""
    if points:
        items = "\n".join(f"    <li>{p}</li>" for p in points)
        points_html = f'<ul style="line-height:2;font-size:15px;">\n{items}\n  </ul>'

    # 도매매 원본 상세설명 (desc.contents) — 이전 버전에선 이 필드가 통째로 누락돼 있었음
    description_block = sanitize_detail_html(product.description or "")
    policy_html = _build_policy_html()

    html = f"""<div style="text-align:center;font-family:sans-serif;">
{img_tags}
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
  {points_html}
</div>
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
{description_block}
</div>
{policy_html}
</div>"""
    return html


def _build_policy_html() -> str:
    """배송/교환·반품/A/S 안내 블록 — 등록 페이로드(register.py의 deliveryInfo·
    afterServiceInfo)와 반드시 같은 값을 써야 하므로 한 곳에서만 만든다. 기본형과
    리메이크형 상세페이지가 각자 다른 문자열을 들고 있으면 나중에 배송비가 바뀔 때
    한쪽만 고치고 잊어버리는 사고가 난다."""
    from ..config import SHIPPING_FEE, FREE_SHIPPING_THRESHOLD, RETURN_DELIVERY_FEE, EXCHANGE_DELIVERY_FEE

    # register.py의 afterServiceGuideContent와 문구를 그대로 맞춘다 — 전화번호를
    # 노출하지 않고 스마트스토어 톡톡으로 문의 채널을 일원화(2026-09).
    return f"""<div style="margin:20px auto;max-width:860px;text-align:left;padding:16px;border-top:1px solid #eee;">
  <h4 style="font-size:15px;margin-bottom:8px;">배송 안내</h4>
  <p style="font-size:14px;color:#555;">기본 배송비 {SHIPPING_FEE:,}원 · {FREE_SHIPPING_THRESHOLD:,}원 이상 구매 시 무료배송</p>
  <h4 style="font-size:15px;margin:16px 0 8px;">교환·반품 안내</h4>
  <p style="font-size:14px;color:#555;">반품 배송비 {RETURN_DELIVERY_FEE:,}원 · 교환 배송비 {EXCHANGE_DELIVERY_FEE:,}원 (단순 변심 기준, 왕복)</p>
  <h4 style="font-size:15px;margin:16px 0 8px;">A/S 안내</h4>
  <p style="font-size:14px;color:#555;">구매 후 문의사항은 스마트스토어 톡톡으로 문의해 주세요.</p>
</div>"""


def remake_detail_html(keyword: str, product: "DomemaeProduct") -> str:
    """
    리메이크(트랙B) 전용 "다시 만들기" — _build_detail_html과 다른 구성으로 상세페이지를
    새로 짠다(2026-09, 리메이크 재생성 요청). 지금까지 리메이크는 "손봐서 등록"이라고만
    안내하고 실제로 손볼 도구(다른 레이아웃 생성)가 없었다.

    원본에 없는 특징은 절대 지어내지 않는다 — 도입 문구는 _extract_points로 원본
    설명에서 뽑은 사실만 근거로 삼는다. ANTHROPIC_API_KEY가 있으면 그 사실들만 주고
    짧은 도입 문구를 새로 쓰게 하고(허위 스펙 금지를 프롬프트에 명시), 없으면
    카테고리 무드어휘 사전(name_optimizer.MOOD_WORDS)만으로 결정적으로 조합한다.
    포인트는 불릿이 아니라 카드형으로 강조 배치해 원본과 눈에 띄게 다른 레이아웃을 만든다.
    """
    points = _extract_points(product.description, limit=4)
    mood_word = _find_mood_word(product.category)
    intro = _generate_remake_intro(keyword, product.category, points) or (
        f"{keyword}, {mood_word}으로 골라보세요." if mood_word else f"{keyword}을(를) 소개합니다."
    )

    img_tags = "".join(f'<img src="{u}" style="width:100%;max-width:860px;" />\n' for u in product.images)

    points_html = ""
    if points:
        cards = "\n".join(
            f'<div style="flex:1 1 200px;background:#f7f7f9;border-radius:10px;padding:14px 16px;">'
            f'<div style="font-size:12px;color:#888;margin-bottom:4px;">POINT {i + 1}</div>'
            f'<div style="font-size:14px;">{p}</div></div>'
            for i, p in enumerate(points)
        )
        points_html = (
            f'<div style="max-width:860px;margin:0 auto;padding:0 16px;">'
            f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:16px 0;">{cards}</div></div>'
        )

    description_block = sanitize_detail_html(product.description or "")
    policy_html = _build_policy_html()

    return f"""<div style="text-align:center;font-family:sans-serif;">
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
  <p style="font-size:17px;font-weight:700;line-height:1.5;">{intro}</p>
</div>
{points_html}
{img_tags}
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
{description_block}
</div>
{policy_html}
</div>"""


def _generate_remake_intro(keyword: str, category: str, points: List[str]) -> str:
    """Claude로 도입 문구 생성 — 준 사실(points) 밖의 내용은 쓰지 말라고 명시한다.
    키 없음/호출 실패/포인트 없음이면 빈 문자열 — 호출부가 결정적 문구로 대체한다."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not points:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        fact_list = "\n".join(f"- {p}" for p in points)
        prompt = f"""네이버 스마트스토어 상세페이지 최상단에 넣을 도입 문구를 2문장 이내로 써줘.

상품 키워드: {keyword}
카테고리: {category}
아래는 실제 상품 설명에서 뽑은 사실이야 — 이 사실만 근거로 써:
{fact_list}

규칙: 위에 없는 효능·인증·수치를 지어내지 마. "최고", "1위" 같은 과장 표현 금지.
문구만 출력해(따옴표·설명 없이)."""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""


def _demo() -> None:
    """실행 가능한 자체 점검 — 상세페이지에 공급사·도매재고·판매가가 새지 않는지,
    포인트가 지어내지 않고 원본에서만 뽑히는지, 배송 안내가 도매매 매입 배송비가
    아니라 실제 구매자 배송정책(config.SHIPPING_FEE)을 쓰는지 확인 (네트워크 호출 없음).
    2026-09: 공급사 ID·도매 재고 노출 문제 + 매입배송비/구매배송비 혼동 문제를
    고친 뒤 재발 방지용으로 추가."""
    from ..sourcing.domemae import DomemaeProduct
    from ..config import SHIPPING_FEE

    product = DomemaeProduct(
        goods_no="12345", name="실리콘주걱", supply_price=2300, retail_price=0,
        min_order_qty=1, stock=3_432_752, supplier="seoul7rsoe", category="주방>조리도구",
        shipping_fee=9999,  # 도매매 매입 배송비 — 구매자 화면에 절대 이 숫자가 나오면 안 됨
        description="<p>실리콘 100% 소재라 인체에 무해합니다.</p><p>미끄럼방지 손잡이로 안전합니다.</p>"
                    "<p>500도 고열에도 변형 없이 오래 씁니다.</p>",
    )
    html = _build_detail_html(product, keyword="실리콘주걱")
    assert "seoul7rsoe" not in html, "공급사 ID가 상세페이지에 노출됨"
    assert "3,432,752" not in html and "3432752" not in html, "도매 재고 수량이 상세페이지에 노출됨"
    assert "실리콘 100% 소재라 인체에 무해합니다" in html, "도매매 원본 설명이 누락됨"
    assert "9,999" not in html, "도매매 매입 배송비가 구매자 화면에 노출됨"
    assert f"{SHIPPING_FEE:,}원" in html, "실제 구매자 배송비 안내가 없음"

    points = _extract_points(product.description)
    assert any("실리콘 100% 소재라 인체에 무해합니다" in p for p in points), points
    assert len(points) <= 3

    empty = DomemaeProduct(
        goods_no="0", name="상품", supply_price=1000, retail_price=0, min_order_qty=1,
        stock=1, supplier="s", category="", shipping_fee=0, description="",
    )
    assert _extract_points(empty.description) == [], "설명이 없는데 포인트를 지어냄"

    # 리메이크 재생성 — API 키 없이도(이 환경 기본값) 크래시 없이 결정적 문구로 동작하고,
    # 도입 문구가 원본에 없는 사실을 지어내지 않는지 확인.
    remake_html = remake_detail_html("실리콘주걱", product)
    assert "seoul7rsoe" not in remake_html and "9,999" not in remake_html
    assert "실리콘 100% 소재라 인체에 무해합니다" in remake_html
    assert "POINT 1" in remake_html, "카드형 포인트 레이아웃이 안 만들어짐"
    assert remake_html != _build_detail_html(product, keyword="실리콘주걱"), "리메이크가 기본형과 동일함(레이아웃이 안 바뀜)"

    no_desc = DomemaeProduct(
        goods_no="0", name="상품", supply_price=1000, retail_price=0, min_order_qty=1,
        stock=1, supplier="s", category="없는카테고리", shipping_fee=0, description="", images=[],
    )
    remake_empty = remake_detail_html("테스트", no_desc)
    assert "테스트" in remake_empty, "포인트·무드어휘가 둘 다 없을 때 기본 도입 문구가 안 나옴"

    # 적대적 입력 — 외부몰 링크·카카오톡 직거래 유도·공급사 전화번호·"국내 1위"·
    # "아토피 개선"이 실제로 통과했던 사례(2026-09 재현). 전부 제거돼야 한다.
    adversarial = DomemaeProduct(
        goods_no="9", name="테스트상품", supply_price=1000, retail_price=0, min_order_qty=1,
        stock=1, supplier="s", category="화장품", shipping_fee=0,
        description=(
            '<p>국내 1위 브랜드! <a href="https://www.coupang.com/vp/products/1">쿠팡에서 더 싸게 사기</a></p>'
            '<p>카카오톡 ID: dome_seller99 로 직접 문의주세요. 전화 010-1234-5678</p>'
            '<p>아토피 개선 효과가 있는 순한 성분입니다.</p>'
        ),
    )
    adv_html = _build_detail_html(adversarial, keyword="테스트상품")
    assert "coupang.com" not in adv_html and "쿠팡" not in adv_html, "외부몰 링크/명칭이 안 걸러짐"
    assert "dome_seller99" not in adv_html, "카카오톡 직거래 유도 아이디가 안 걸러짐"
    assert "010-1234-5678" not in adv_html, "공급사 전화번호가 안 걸러짐"
    assert "1위" not in adv_html, "과장 표현이 안 걸러짐"
    assert "아토피" not in adv_html, "의약품 오인 표현이 안 걸러짐"
    adv_points = _extract_points(adversarial.description)
    assert not any("쿠팡" in p or "1위" in p or "아토피" in p or "010-" in p for p in adv_points), \
        f"홍보/연락처 문구가 상품 포인트로 승격됨: {adv_points}"

    print("content._demo self-check OK")


if __name__ == "__main__":
    _demo()
