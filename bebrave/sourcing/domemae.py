"""
도매꾹 OpenAPI 연동 — 도매매(supply) 상품 검색 및 상세 조회.

엔드포인트: https://domeggook.com/ssl/api/
  - mode=getItemList : 키워드 상품 검색 (목록)
  - mode=getItemView : 상품 상세 조회 (이미지·설명·재고·배송비)

API 키: .env의 DOMEMAE_API_KEY (오픈마켓/플랫폼 연동 키)
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE = "https://domeggook.com/ssl/api/"


# ── 데이터 모델 ────────────────────────────────────────────────────────────────

@dataclass
class DomemaeProduct:
    goods_no: str
    name: str
    supply_price: int          # 도매가 (원) — 최소주문수량 기준 최저가
    retail_price: int          # 소비자가 (원), 참고용
    min_order_qty: int         # 최소 주문 수량
    stock: int                 # 재고 (getItemView에서만 정확히 조회됨)
    supplier: str              # 공급업체 ID
    category: str              # 카테고리
    shipping_fee: int = 0      # 공급업체 출고 배송비
    url: str = ""
    images: list = field(default_factory=list)
    detail_image_url: str = ""
    description: str = ""
    origin_country: str = ""   # 원산지 (도매매 detail.country) — 빈 값이면 미확인
    option_group_name: str = ""  # 옵션 축 이름 (예: "색상") — 옵션 없으면 빈 값
    options: list = field(default_factory=list)  # [{"name","extra_price","stock"}] — 단일 축만 지원

    @property
    def main_image(self) -> str:
        return self.images[0] if self.images else ""

    def summary(self) -> str:
        return (
            f"[도매매] {self.name[:30]}\n"
            f"  도매가: {self.supply_price:,}원 | 최소주문: {self.min_order_qty}개 | 재고: {self.stock}개\n"
            f"  공급사: {self.supplier}"
        )


@dataclass
class DomemaeSearchResult:
    keyword: str
    total: int
    products: List[DomemaeProduct] = field(default_factory=list)
    cheapest: Optional[DomemaeProduct] = None  # 최저 도매가 상품

    def summary(self) -> str:
        if not self.products:
            return f"[도매매] '{self.keyword}' 검색 결과 없음"
        p = self.cheapest or self.products[0]
        return (
            f"[도매매] '{self.keyword}' 검색결과 {self.total}개\n"
            f"  최저 도매가: {p.supply_price:,}원 ({p.supplier})"
        )


# ── 가격 파싱 헬퍼 ─────────────────────────────────────────────────────────────

def _parse_price(raw) -> int:
    """
    도매꾹 price 필드 파싱.
    단일값: "4130" → 4130
    수량별: "5+2100|20+1900|50+1800" → 최소수량(5개) 기준 가격 2100
    """
    if not raw:
        return 0
    s = str(raw).strip()
    # 수량별 가격: "수량+가격|수량+가격" 형식
    if "|" in s or "+" in s:
        # 첫 번째 구간(최소수량) 가격 추출
        first = s.split("|")[0]
        parts = first.split("+")
        price_str = parts[-1] if len(parts) >= 2 else parts[0]
        return int(re.sub(r"[^\d]", "", price_str) or 0)
    return int(re.sub(r"[^\d]", "", s) or 0)


def _get_key(api_key: str = "") -> str:
    key = api_key or os.environ.get("DOMEMAE_API_KEY", "")
    if not key:
        raise ValueError(".env 파일에 DOMEMAE_API_KEY를 설정하세요.")
    return key


# ── 상품 타입 정합성 매칭 ───────────────────────────────────────────────────────
# 도매매 자체 검색(kw=keyword)은 느슨한 텍스트 매칭이라, 검색어와 무관한 카테고리
# 상품(예: '발각질연화제' 검색에 각질제거 도구가 섞여 나옴)까지 반환한다.
# 무조건 최저가를 고르면 크림 수요에 스크래퍼를 매칭하는 식의 오류가 생기므로,
# 네이버 상위 상품명과 토큰이 겹치는 후보만 "같은 상품 타입"으로 간주해 그 안에서
# 최저가를 고른다.

_GENERIC_STOPWORDS = {
    "세트", "1개", "2개", "3개", "무료배송", "정품", "국산", "당일발송",
    "공식", "몰", "New", "new", "베스트", "인기", "추천", "특가", "할인",
    "여성", "남성", "선물", "커플", "리필",
}

# 신체부위/용도 명사(발·뒤꿈치·각질 등)는 크림이든 도구든 똑같이 등장해서
# "같은 상품 타입"의 증거가 되지 못한다. 실제 형태를 구분하는 건 제형/재질/
# 기구 명사뿐이므로, 매칭 판단은 이 사전에 속한 단어의 교집합으로만 한다.
_FORM_WORDS = {
    # 화장품/소모품 제형
    "크림", "연고", "젤", "로션", "오일", "워터", "스프레이", "앰플",
    "팩", "마스크", "스틱", "밤", "폼", "클렌저", "클렌징", "토너",
    "에센스", "세럼", "스크럽", "소금", "입욕제", "패치", "패드",
    "필링", "가글", "미스트", "분무기",
    # 도구/기기
    # 글자 하나짜리 "기"는 뺀다 — "~기"로 끝나는 모든 단어(예: 만들기)에
    # 부분일치로 걸려서 무관한 상품을 "형태 일치"로 오판정했다(2026-08 발견).
    "제거기", "마사지기", "브러쉬", "브러시", "롤러", "타올",
    "타월", "수세미", "장갑", "집게", "클립", "밴드", "보호대",
    "교정기", "스톤", "나이프", "바렌", "줄", "가위", "핀셋", "덧신",
    "양말", "괄사", "폼롤러", "볼", "스케일러", "빗", "안대", "쿠션",
    "깔창", "테이프",
}


def _tokenize(text: str) -> set:
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", text or "")
    return {t for t in tokens if len(t) >= 2 and t not in _GENERIC_STOPWORDS}


def _form_signals(tokens: set) -> set:
    """
    토큰 집합에서 '제형/형태' 신호만 추출. 부분일치도 잡는다
    (예: '발각질제거기' 토큰 안에 '제거기'가 포함).
    """
    return {form for form in _FORM_WORDS if any(form in t for t in tokens)}


def _matched_candidates(
    naver_titles: List[str],
    products: List["DomemaeProduct"],
    min_overlap: int = 1,
) -> List["DomemaeProduct"]:
    """네이버 상위 상품명과 '제형/형태 신호'가 겹치는 도매매 후보 전체(가격순).

    형태 사전(_FORM_WORDS)은 뷰티/셀프케어 어휘 위주라 다른 카테고리(문구,
    반려동물, 인테리어 등)는 실제로 맞는 상품도 사전에 없는 단어라 못 잡는
    경우가 대부분이었다(2026-08 실측: 불확실 판정의 78%가 여기 해당).
    검색 키워드 자체가 상품명에 그대로 들어있으면 — 그 자체로 사전 없이도
    믿을 만한 증거이므로 — 형태 단어 매칭과 별개로 추가 인정한다.
    """
    naver_tokens = set()
    for t in naver_titles:
        naver_tokens |= _tokenize(t)
    naver_forms = _form_signals(naver_tokens)
    keyword_text = "".join(naver_titles).replace(" ", "")
    # "~만들기/재료/DIY"류는 완제품이 아니라 재료·키트 상품명에 그 문구가 그대로
    # 들어가는 경우가 흔해서(예: "팔찌만들기"가 "비즈팔찌만들기 끈"에 포함) 키워드
    # 그대로 포함 규칙에서 제외한다 — "우레탄 줄" 오탐 사례(2026-08).
    _is_craft_keyword = any(s in keyword_text for s in ("만들기", "재료", "DIY", "diy"))

    def _is_match(p: "DomemaeProduct") -> bool:
        if len(naver_forms & _form_signals(_tokenize(p.name))) >= min_overlap:
            return True
        if not _is_craft_keyword and len(keyword_text) >= 2 and keyword_text in p.name.replace(" ", ""):
            return True
        return False

    matched = [p for p in products if _is_match(p)]
    return sorted(matched, key=lambda p: p.supply_price)


def find_matching_product(
    naver_titles: List[str],
    products: List["DomemaeProduct"],
    min_overlap: int = 1,
):
    """
    네이버 상위 상품명과 '제형/형태 신호'가 겹치는 도매매 후보 중 최저가를 선택.
    신체부위·용도 명사(발·뒤꿈치·각질 등)는 형태 구분에 쓸모가 없으므로 제외하고,
    크림/연고 vs 도구/기기처럼 실제 상품 형태를 나타내는 단어만 비교한다.

    Returns:
        (선택 상품 또는 None, matched: bool)
        matched=False면 형태 일치를 확인 못 한 채 고른 값이므로 수동 확인 필요.
    """
    if not products:
        return None, False

    matched_candidates = _matched_candidates(naver_titles, products, min_overlap)
    if matched_candidates:
        return matched_candidates[0], True

    # 겹치는 후보가 없으면 최저가는 참고용으로만 반환하고 불확실 플래그
    return min(products, key=lambda p: p.supply_price), False


def find_all_matches(
    naver_titles: List[str],
    products: List["DomemaeProduct"],
    min_overlap: int = 1,
    top_n: int = 5,
) -> List["DomemaeProduct"]:
    """
    검증된 키워드 하나에 대해 형태가 일치하는 도매매 후보를 가격순으로 여러 개
    반환한다. 이미 수요가 확인된 키워드 밑에서 디자인·색상이 다른 변형 상품을
    빠르게 카탈로그에 추가할 때 사용 — 신규 키워드 발굴 없이 등록 물량을 늘리는
    용도 (2026-07 "변형 카탈로그" 전략).
    """
    if not products:
        return []
    return _matched_candidates(naver_titles, products, min_overlap)[:top_n]


# ── 상품 파싱 ──────────────────────────────────────────────────────────────────

def _parse_list_item(item: dict) -> DomemaeProduct:
    """getItemList 응답의 item 하나 → DomemaeProduct."""
    # 배송비 파싱 (deli 객체 안에 있음)
    deli = item.get("deli", {}) or {}
    dome_deli = deli.get("dome", deli.get("supply", {})) or {}
    shipping_fee = _parse_price(dome_deli.get("fee", 0))

    return DomemaeProduct(
        goods_no=str(item.get("no", "")),
        name=item.get("title", ""),
        supply_price=_parse_price(item.get("price", 0)),
        retail_price=0,  # getItemList에는 소비자가 없음
        min_order_qty=int(item.get("unitQty", 1) or 1),
        stock=0,         # getItemList에는 재고 없음 (getItemView에서 조회)
        supplier=str(item.get("id", "")),
        category="",
        shipping_fee=shipping_fee,
        url=f"https://www.domeggook.com/main/goods/view.php?no={item.get('no','')}",
        images=[item.get("thumb", "")] if item.get("thumb") else [],
    )


def _parse_options(raw_select_opt) -> tuple:
    """
    selectOpt(JSON 문자열) → (옵션축 이름, [{"name","extra_price","stock"}]).
    조합형(type=="combination")이면서 옵션 축이 정확히 1개일 때만 파싱하고,
    그 외(옵션 없음/2축 이상 조합)는 ("", [])를 반환한다.
    """
    if not raw_select_opt:
        return "", []
    try:
        opt = json.loads(raw_select_opt) if isinstance(raw_select_opt, str) else raw_select_opt
    except Exception:
        return "", []

    opt_set = opt.get("set") or []
    if opt.get("type") != "combination" or len(opt_set) != 1:
        return "", []

    group_name = opt_set[0].get("name", "")
    options = []
    for combo in (opt.get("data") or {}).values():
        name = combo.get("name", "")
        if not name:
            continue
        options.append({
            "name": name,
            "extra_price": _parse_price(combo.get("supPrice", 0)),
            "stock": int(combo.get("qty", 0) or 0),
        })
    # 선택지가 1개뿐이면 실질적으로 옵션이 아님(예: "진레드색상만 출고가능")
    if len(options) < 2:
        return "", []
    return group_name, options


def _parse_view_item(data: dict) -> DomemaeProduct:
    """getItemView 응답의 domeggook 객체 → DomemaeProduct."""
    basis   = data.get("basis", {}) or {}
    price_d = data.get("price", {}) or {}
    qty_d   = data.get("qty", {}) or {}
    deli_d  = data.get("deli", {}) or {}
    thumb_d = data.get("thumb", {}) or {}
    desc_d  = data.get("desc", {}) or {}
    detail  = data.get("detail", {}) or {}

    # 가격: supply > dome 순으로 시도
    raw_price = price_d.get("supply") or price_d.get("dome") or 0
    supply_price = _parse_price(raw_price)

    # 대표이미지: original 1장만 사용 (large/small은 같은 사진의 해상도 변형일 뿐,
    # 서로 다른 사진이 아니므로 "여러 장"인 것처럼 취급하지 않음 — 2026-07-12 확인된 버그 수정)
    images = []
    main_thumb = thumb_d.get("original") or thumb_d.get("large") or thumb_d.get("small")
    if main_thumb:
        images.append(main_thumb)

    # 상세설명 HTML 안에 실제 갤러리 사진이 <img> 태그로 들어있는 경우가 많음 → 추출해서 추가
    desc_contents = desc_d.get("contents", "")
    for img_url in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', desc_contents):
        if img_url not in images:
            images.append(img_url)

    # 배송비
    supply_deli = deli_d.get("supply", deli_d.get("dome", {})) or {}
    shipping_fee = _parse_price(supply_deli.get("fee", 0))

    # 카테고리 — 실제 응답 구조는 category.current.name(최종 분류명) +
    # category.parents.elem[](상위 경로) — name1/name2/name3 키는 존재하지 않았음
    # (2026-07-12 확인된 버그: 항상 빈 문자열을 반환하고 있었음)
    cat_d = data.get("category", {}) or {}
    current_name = (cat_d.get("current") or {}).get("name", "")
    parent_names = [e.get("name", "") for e in (cat_d.get("parents") or {}).get("elem", [])]
    category = ">".join([n for n in (parent_names + [current_name]) if n])

    # 제조국
    country = detail.get("country", "")

    # 옵션 — selectOpt는 dict가 아니라 JSON을 담은 문자열로 내려옴 (실API 확인).
    # 옵션 축(색상+사이즈 등)이 2개 이상인 조합형은 조합 폭발을 안전하게 매핑할
    # 근거가 없어 지원하지 않음 — 단일 축(색상만 또는 사이즈만)만 옵션으로 등록하고,
    # 그 외에는 옵션 없는 단일상품으로 취급 (기존 동작과 동일, 회귀 없음).
    option_group_name, options = _parse_options(data.get("selectOpt", ""))

    return DomemaeProduct(
        goods_no=str(basis.get("no", "")),
        name=basis.get("title", ""),
        supply_price=supply_price,
        retail_price=0,
        min_order_qty=int(qty_d.get("domeMoq", qty_d.get("supplyUnit", 1)) or 1),
        stock=int(qty_d.get("inventory", 0) or 0),
        supplier=data.get("seller", {}).get("id", ""),
        category=category,
        shipping_fee=shipping_fee,
        url=f"https://www.domeggook.com/main/goods/view.php?no={basis.get('no','')}",
        images=images,
        detail_image_url="",  # desc.contents에 HTML로 포함됨
        origin_country=country,
        option_group_name=option_group_name,
        options=options,
        # desc.notice는 연휴/배송 공지 등 상품과 무관한 안내문이라 폴백으로 쓰면 안 됨
        # (2026-07-12 확인된 버그 수정 — 이전엔 contents 없으면 notice가 상세설명으로 들어갔음)
        description=desc_contents,
    )


# ── 공개 API ───────────────────────────────────────────────────────────────────

def search_products(
    keyword: str,
    limit: int = 10,
    api_key: str = "",
) -> DomemaeSearchResult:
    """
    키워드로 도매매 상품 검색.
    cheapest: 최저 도매가 상품 자동 선택.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests")

    key = _get_key(api_key)
    params = {
        "ver": "4.1", "mode": "getItemList",
        "aid": key, "market": "supply",
        "om": "json", "kw": keyword, "sz": limit,
    }
    resp = requests.get(_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    dome = data.get("domeggook", {})
    header = dome.get("header", {})
    total = int(header.get("numberOfItems", 0))

    raw_list = dome.get("list", {})
    if isinstance(raw_list, dict):
        raw_list = raw_list.get("item", [])
    if not isinstance(raw_list, list):
        raw_list = []

    products = []
    for item in raw_list:
        try:
            products.append(_parse_list_item(item))
        except Exception:
            continue

    cheapest = min(products, key=lambda p: p.supply_price) if products else None

    return DomemaeSearchResult(
        keyword=keyword,
        total=total,
        products=products,
        cheapest=cheapest,
    )


def fetch_product_detail(
    goods_no: str,
    api_key: str = "",
) -> DomemaeProduct:
    """
    상품번호로 상세 정보 조회 (이미지, 재고, 배송비, 상세설명 포함).
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests")

    key = _get_key(api_key)
    params = {
        "ver": "4.1", "mode": "getItemView",
        "aid": key, "market": "supply",
        "om": "json", "no": goods_no,
    }
    resp = requests.get(_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    dome = data.get("domeggook", data)
    if "errors" in dome:
        raise ValueError(f"도매꾹 API 오류: {dome['errors']}")

    return _parse_view_item(dome)
