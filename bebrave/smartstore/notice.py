"""
상품정보제공고시 — 유형 결정 + 필수 항목 자동 채우기.

이전 버전은 모든 상품을 ETC(기타 재화)로 등록하고 etc 하위에 4개 항목만 넣고 있었다.
실제로는 공정위 "전자상거래 상품정보제공고시"에 맞춰 36개 유형이 있고, 유형마다
채워야 할 항목이 다르다. 게다가 ETC조차 필수 항목 6개 중 4개만 채우고 있어서
법에 의한 인증 사항(certificateDetails)과 A/S 책임자(afterServiceDirector)가
누락된 상태로 등록되고 있었다 (2026-08-10 실등록 조회로 확인).

유형별 항목 스펙을 API가 그대로 내려주므로 하드코딩하지 않고 받아서 채운다.
API: GET https://api.commerce.naver.com/external/v1/products-for-provided-notice

항목 값은 도매매 원본에서 찾을 수 있는 것(제조사·제조국·모델명)을 우선 쓰고,
찾을 수 없는 것은 "상세페이지 참조"로 채운다 — 빈 값으로 두면 등록이 거부된다.
"""
import json
import time
from pathlib import Path
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_NOTICE_URL = "https://api.commerce.naver.com/external/v1/products-for-provided-notice"
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "naver_notice_specs_cache.json"
_CACHE_TTL_SECONDS = 30 * 24 * 3600

_FALLBACK = "상세페이지 참조"

# TODO(#1): 더미 연락처 — 판매 개시 전 실제 번호로 교체 필요.
# 고시의 A/S 책임자·소비자 상담 번호와 afterServiceInfo가 같은 값을 써야 하므로 여기 모아둔다.
CS_PHONE_NUMBER = "010-0000-0000"

# 카테고리 경로에 이 단어가 들어가면 해당 고시유형으로 본다. 위에서부터 먼저 맞는 것을 쓰므로
# 구체적인 것이 앞에 와야 한다. 확신이 없는 카테고리는 일부러 비워두고 ETC로 떨어뜨린다 —
# 틀린 유형을 쓰면 엉뚱한 고시 항목이 소비자에게 표시되기 때문.
_TYPE_KEYWORDS = (
    ("KITCHEN_UTENSILS", ("주방", "조리", "식기", "냄비", "프라이팬", "주걱", "도마",
                          "수저", "컵", "텀블러", "밀폐용기", "보관용기", "커트러리")),
    ("BAG",              ("가방", "백팩", "크로스백", "숄더백", "파우치", "지갑")),
    ("SHOES",            ("신발", "운동화", "구두", "슬리퍼", "샌들", "부츠")),
    ("WEAR",             ("의류", "티셔츠", "셔츠", "바지", "원피스", "아우터", "코트",
                          "양말", "레깅스", "속옷", "잠옷", "니트")),
    ("FASHION_ITEMS",    ("패션잡화", "패션소품", "모자", "벨트", "장갑", "머플러",
                          "스카프", "우산", "양산", "헤어")),
    ("JEWELLERY",        ("주얼리", "귀금속", "반지", "목걸이", "귀걸이", "팔찌")),
    ("COSMETIC",         ("화장품", "스킨", "로션", "에센스", "세럼", "마스크팩", "클렌징")),
    ("FURNITURE",        ("가구", "책상", "의자", "선반", "수납장", "옷장", "서랍")),
    ("SLEEPING_GEAR",    ("침구", "이불", "베개", "매트리스", "패드")),
    ("SPORTS_EQUIPMENT", ("스포츠", "운동기구", "헬스", "요가", "등산", "캠핑")),
    ("KIDS",             ("유아", "아동", "완구", "장난감", "출산")),
    ("BOOKS",            ("도서", "책", "서적")),
)


def _load_notice_specs(access_token: str) -> List[dict]:
    """캐시가 있고 신선하면 재사용, 아니면 API로 유형별 항목 스펙을 가져와 캐시."""
    if _CACHE_PATH.exists():
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age < _CACHE_TTL_SECONDS:
            try:
                return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(_NOTICE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    resp.raise_for_status()
    specs = resp.json()

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(specs, ensure_ascii=False), encoding="utf-8")
    return specs


def resolve_notice_type(category_path: str, product_name: str = "") -> str:
    """
    카테고리 경로(+상품명)로 고시유형을 결정. 확실한 매치가 없으면 "ETC".

    ETC로 떨어지는 건 실패가 아니라 "기타 재화"라는 유효한 유형이지만, 의류를 ETC로
    올리면 소재·치수·세탁방법 같은 고시 항목이 빠지므로 호출부에서 로그로 알려줄 것.
    """
    haystack = f"{category_path} {product_name}"
    for notice_type, keywords in _TYPE_KEYWORDS:
        if any(k in haystack for k in keywords):
            return notice_type
    return "ETC"


def _field_value(field_name: str, product) -> Optional[str]:
    """항목 이름 → 도매매/상품 데이터에서 찾은 값. 못 찾으면 None(호출부가 폴백 처리)."""
    from .register import _clean  # 도매매의 "해당없음" 류 자리표시자 제거

    if field_name == "itemName":
        return product.name
    if field_name == "modelName":
        return _clean(getattr(product, "model", "")) or product.domemae_goods_no or None
    if field_name == "manufacturer":
        return _clean(getattr(product, "manufacturer", "")) or None
    if field_name == "producer":
        # 제조국 — 도매매 원산지 표기("수입산_아시아_중국")의 마지막 조각을 쓴다
        raw = getattr(product, "origin_country", "") or ""
        return raw.split("_")[-1] if raw else None
    if field_name in ("afterServiceDirector", "customerServicePhoneNumber"):
        return CS_PHONE_NUMBER
    return None


def build_provided_notice(product, access_token: str, notice_type: str = "") -> dict:
    """
    productInfoProvidedNotice 전체를 생성. 유형에 정의된 String 항목을 빠짐없이 채운다.

    YearMonth/LocalDate/Boolean 항목은 값을 지어내면 허위 표시가 되므로 생략한다
    (제조연월·유통기한·수입신고 여부 등 — 도매매가 주지 않는 정보). 등록이 거부되면
    그때 해당 항목이 필수임이 확인되는 것이니 그 시점에 대응할 것.
    """
    ntype = notice_type or resolve_notice_type(
        getattr(product, "domemae_category", "") or getattr(product, "keyword", ""),
        product.name,
    )

    specs = _load_notice_specs(access_token)
    spec = next((s for s in specs if s.get("productInfoProvidedNoticeType") == ntype), None)
    if spec is None:
        ntype, spec = "ETC", next(s for s in specs if s.get("productInfoProvidedNoticeType") == "ETC")

    fields = {}
    for f in spec.get("productInfoProvidedNoticeContents", []):
        if f.get("fieldType") != "String":
            continue  # 날짜·불리언은 지어내지 않음 (위 docstring 참고)
        name = f["fieldName"]
        value = _field_value(name, product) or _FALLBACK
        max_len = f.get("fieldMaxLength")
        if max_len and len(value) > max_len:
            value = value[:max_len]
        fields[name] = value

    # 고시유형 코드는 그대로 쓰되, 하위 노드 이름은 소문자 카멜케이스 규칙을 따른다
    # (예: KITCHEN_UTENSILS → kitchenUtensils). ETC는 etc.
    node = _node_name(ntype)
    return {"productInfoProvidedNoticeType": ntype, node: fields}


def _node_name(notice_type: str) -> str:
    """KITCHEN_UTENSILS → kitchenUtensils, ETC → etc, WEAR → wear."""
    head, *rest = notice_type.lower().split("_")
    return head + "".join(w.capitalize() for w in rest)
