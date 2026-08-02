"""
Claude API를 활용한 상품 콘텐츠 자동 생성.

- 상품명: 네이버쇼핑 SEO 가이드 기준 (동의어/유의어 반복 금지, 50자 미만, 단어 경계 유지)
- 상세설명: 도매매 원본 상세설명(desc.contents) + 이미지 + 핵심 정보
- 검색어 태그: 노출에 유리한 5개 내외 태그 생성

가이드 출처 (2026-07-12 조사): 네이버쇼핑 상위노출 체크리스트 — 상품명은 동의어·유의어
중복, 판매조건·홍보문구·카테고리명·판매처명 포함 금지. 브랜드/제조사는 전용 필드에만
정확히 입력. 검색어 태그는 상품과 무관한 걸 억지로 채우는 것보다 5~7개 정도가 유리.

환경변수:
  ANTHROPIC_API_KEY
"""
import os
import re
from typing import TYPE_CHECKING, List

from .name_optimizer import BANNED_PROMO_WORDS, MAX_NAME_LEN as _MAX_NAME_LEN
from .name_optimizer import _find_mood_word, optimize_name, truncate_at_word_boundary as _truncate_at_word_boundary
from ..sourcing.keyword_tool import fetch_related_keywords

if TYPE_CHECKING:
    from ..sourcing.domemae import DomemaeProduct


def _tokens(text: str) -> set:
    return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text or ""))


def _demand_tags(keyword: str, product: "DomemaeProduct", limit: int = 3) -> List[str]:
    """
    네이버 검색광고 API로 실제 월검색수가 있는 관련 키워드를 태그로 채택.
    2026-08 시뮬레이션(50건 실측)으로 확인: 원본 제목/카테고리와 단어가 겹치는 것만
    걸러서 검색량 순으로 골라야 무관한 대형 키워드(예: "마사지")가 안 섞임.
    API 실패/키 미설정이면 조용히 빈 리스트 — 호출부가 다른 태그로 채운다.
    """
    try:
        related = fetch_related_keywords(keyword, limit=30)
    except Exception:
        return []

    # 부분일치(substring) 기준 — 관련 키워드가 붙여쓰기 복합어("두피브러쉬")로 오는 경우가
    # 많아서, 원본 제목의 개별 토큰("두피")과 정확히 같은 통짜 토큰이어야 한다는 조건(교집합)은
    # 대부분 걸러버림. base_tokens 각각이 관련 키워드 문자열 "안에" 들어있는지로 완화
    # (2026-08 50건 실측에서 발견 — 이 조건 때문에 매칭률이 실제보다 훨씬 낮게 나왔었음).
    base_tokens = _tokens(product.name) | _tokens(product.category)
    relevant = [
        r for r in related
        if r.monthly_total > 0
        and r.keyword != keyword
        and r.keyword not in BANNED_PROMO_WORDS
        and (
            keyword in r.keyword
            or r.keyword in product.name
            or any(bt in r.keyword for bt in base_tokens)
        )
    ]
    relevant.sort(key=lambda r: r.monthly_total, reverse=True)
    return [r.keyword for r in relevant[:limit]]


def generate_product_content(
    keyword: str,
    product: "DomemaeProduct",
    sale_price: int,
) -> dict:
    """
    AI 기반 상품명 + 상세설명 + 검색어 태그 생성.

    Returns:
        {"name": str, "detail_content": str, "tags": List[str]}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if api_key:
        return _generate_with_claude(keyword, product, sale_price, api_key)
    else:
        # Claude API 키 없을 때 기본 템플릿 사용
        return _generate_fallback(keyword, product, sale_price)


def _generate_with_claude(
    keyword: str,
    product: "DomemaeProduct",
    sale_price: int,
    api_key: str,
) -> dict:
    try:
        import anthropic
    except ImportError:
        return _generate_fallback(keyword, product, sale_price)

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
- 45자 이내
- 핵심 키워드를 앞에 배치
- 동의어·유의어를 나열하지 말 것 (예: "우산 양산 양우산 자동우산"처럼 같은 뜻 반복 금지 — 어뷰징으로 간주되어 검색 노출에 불리함)
- 브랜드/제조사(있는 경우) + 상품유형 + 핵심 속성(색상/소재/수량 등) 순서로 간결하게 구성
- 배송·할인·판매조건·홍보 문구, 카테고리명, 판매처명 포함 금지
{mood_hint}
- 특수문자 최소화
- 상품명만 출력 (설명 없이)"""

    name_msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": name_prompt}],
    )
    optimized_name = _truncate_at_word_boundary(name_msg.content[0].text.strip(), _MAX_NAME_LEN)

    detail_content = _build_detail_html(product, sale_price, keyword)
    tags = _generate_tags(keyword, product)

    return {"name": optimized_name, "detail_content": detail_content, "tags": tags}


def _generate_fallback(
    keyword: str,
    product: "DomemaeProduct",
    sale_price: int,
) -> dict:
    """Claude API 없을 때 기본 상품명 + 상세설명 생성."""
    name = optimize_name(keyword, product.name, category=product.category)
    detail_content = _build_detail_html(product, sale_price, keyword)
    tags = _generate_tags(keyword, product)
    return {"name": name, "detail_content": detail_content, "tags": tags}


def _generate_tags(keyword: str, product: "DomemaeProduct") -> List[str]:
    """
    검색어 태그 후보 생성 (code 없이 text만 등록 — 네이버 공식 가이드상 code 생략 가능).
    우선순위: ① 소싱 키워드 자체 ② 실제 월검색수가 있는 관련 키워드(수요기반, 최대 3개)
    ③ 그래도 자리가 남으면 카테고리/원본 제목에서 채움. 상품과 무관한 단어는 억지로 안 채움.
    """
    seen = set()
    tags = []

    def _add(t: str, cap: int) -> bool:
        t = t.strip()
        if t and t not in seen and t not in BANNED_PROMO_WORDS:
            seen.add(t)
            tags.append(t)
        return len(tags) >= cap

    if _add(keyword, 5):
        pass

    for t in _demand_tags(keyword, product, limit=3):
        if _add(t, 5):
            break

    if len(tags) < 5 and product.category:
        for t in product.category.split(">")[-2:]:
            if _add(t, 5):
                break

    if len(tags) < 5:
        for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", product.name):
            if _add(w, 5):
                break

    return tags[:5]


def _build_detail_html(product: "DomemaeProduct", sale_price: int, keyword: str) -> str:
    """도매매 이미지 + 실제 상세설명(desc.contents) + 기본 정보로 상세설명 HTML 구성."""
    img_tags = ""
    for img_url in product.images:
        img_tags += f'<img src="{img_url}" style="width:100%;max-width:860px;" />\n'

    shipping_note = (
        f"배송비: {product.shipping_fee:,}원"
        if product.shipping_fee
        else "배송비: 조건부 무료"
    )

    # 도매매 원본 상세설명 (desc.contents) — 이전 버전에선 이 필드가 통째로 누락돼 있었음
    description_block = product.description or ""

    html = f"""<div style="text-align:center;font-family:sans-serif;">
{img_tags}
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
  <h3 style="font-size:18px;">{product.name}</h3>
  <ul style="line-height:2;">
    <li>판매가: {sale_price:,}원</li>
    <li>공급사: {product.supplier}</li>
    <li>{shipping_note}</li>
    <li>재고: {product.stock}개</li>
  </ul>
</div>
<div style="margin:20px auto;max-width:860px;text-align:left;padding:0 16px;">
{description_block}
</div>
</div>"""
    return html
