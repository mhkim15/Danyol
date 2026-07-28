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

from .name_optimizer import MAX_NAME_LEN as _MAX_NAME_LEN
from .name_optimizer import optimize_name, truncate_at_word_boundary as _truncate_at_word_boundary

if TYPE_CHECKING:
    from ..sourcing.domemae import DomemaeProduct


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
    name = optimize_name(keyword, product.name)
    detail_content = _build_detail_html(product, sale_price, keyword)
    tags = _generate_tags(keyword, product)
    return {"name": name, "detail_content": detail_content, "tags": tags}


def _generate_tags(keyword: str, product: "DomemaeProduct") -> List[str]:
    """
    검색어 태그 후보 생성 (code 없이 text만 등록 — 네이버 공식 가이드상 code 생략 가능).
    상품과 무관한 단어를 억지로 채우지 않고, 실제로 연관된 것만 최대 5개.
    """
    candidates = [keyword]
    if product.category:
        candidates.extend(product.category.split(">")[-2:])  # 도매매 분류의 마지막 1~2단계
    # 원본 제목에서 2글자 이상 단어 중 키워드와 겹치지 않는 것 몇 개 추가
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", product.name):
        if w not in candidates:
            candidates.append(w)
        if len(candidates) >= 5:
            break

    seen = set()
    tags = []
    for t in candidates:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
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
