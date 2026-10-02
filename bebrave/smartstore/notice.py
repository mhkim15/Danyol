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
import os
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

# .env의 CS_PHONE_NUMBER를 씀 — 미설정이면 더미값으로 폴백하되, 등록/소급수정 시
# 항상 경고를 띄워서 실번호 없이 조용히 나가는 걸 막는다 (고시의 A/S 책임자·소비자
# 상담번호와 afterServiceInfo가 같은 값을 써야 하므로 여기 한 곳에 모아둔다).
# register.py·main.py가 더미 여부를 판정할 때도 같은 값을 참조하도록 공개 상수로 둔다.
DUMMY_CS_PHONE_NUMBER = "010-0000-0000"
CS_PHONE_NUMBER = os.environ.get("CS_PHONE_NUMBER", "") or DUMMY_CS_PHONE_NUMBER
if CS_PHONE_NUMBER == DUMMY_CS_PHONE_NUMBER:
    print("[경고] CS_PHONE_NUMBER 미설정 — 등록 상품에 더미 연락처(010-0000-0000)가 나갑니다. .env에 실제 번호를 넣으세요.")

# 수입자 — 도매매가 수입자를 밝히지 않은 수입산은 판매자 상호를 쓴다(2026-10 결정).
SELLER_BUSINESS_NAME = os.environ.get("SELLER_BUSINESS_NAME", "").strip()

# ── "상세페이지 참조"를 써도 되는 항목과 안 되는 항목 (2026-10) ───────────────────
# 기준: 상세페이지에 그 정보가 실제로 있을 때만 "참조"가 사실이다. 아래 두 묶음은
# 상세페이지와 무관하게 값을 정할 수 있으므로 "참조"를 절대 쓰지 않는다.
#
# 인증·허가 — 2023년 고시 개정으로 "상세정보 참조"로 대신할 수 없다고 명시됐다. 인증 대상
# 품목(전기·어린이·생활화학·화장품)은 등록 단계에서 이미 막으므로 남은 상품은 "해당없음"이 사실이다.
CERT_FIELDS = {"certificateDetails", "certificationType", "licenceNo", "safeCriterionNo",
               "approvalNumber", "roadWorthyCertification"}
CERT_NOT_APPLICABLE = "해당없음"
# 우리가 가진 데이터로 직접 채우는 항목 — 상품명·모델명·제조자·수입자·제조국·연락처.
# 도매 상세페이지에 제조자·수입자가 적힌 경우는 거의 없어 "참조"는 대개 거짓이 된다.
DIRECT_FIELDS = {"itemName", "productName", "modelName", "manufacturer", "importer", "producer",
                 "afterServiceDirector", "customerServicePhoneNumber", "warrantyPolicy"} | CERT_FIELDS
# 품질보증기준은 어느 상품에나 사실인 표준 문구로 채운다.
WARRANTY_TEXT = "관련 법 및 소비자분쟁해결기준에 따름"
# 나머지(소재·색상·크기·구성품·주의사항 등)는 "상세페이지 참조"를 쓰되, 도매 원본 상세페이지가
# 잘리지 않고 전부 실렸을 때만 사실이다 — 등록 항목 점검이 "확인 필요"로 표시한다.

# 카테고리 경로에 이 단어가 들어가면 해당 고시유형으로 본다. 위에서부터 먼저 맞는 것을 쓰므로
# 구체적인 것이 앞에 와야 한다. 확신이 없는 카테고리는 일부러 비워두고 ETC로 떨어뜨린다 —
# 틀린 유형을 쓰면 엉뚱한 고시 항목이 소비자에게 표시되기 때문.
# 침구는 가구보다 앞 — 베개가 "가구/인테리어>침구단품" 아래라 가구로 먼저 걸렸다(2026-09).
# "헤어"는 뺐다 — 헤어브러시·헤어케어까지 패션잡화로 걸렸다. 머리 장식만 패션잡화다.
_TYPE_KEYWORDS = (
    ("KITCHEN_UTENSILS", ("주방", "조리", "식기", "냄비", "프라이팬", "주걱", "도마",
                          "수저", "컵", "텀블러", "밀폐용기", "보관용기", "커트러리")),
    ("BAG",              ("가방", "백팩", "크로스백", "숄더백", "파우치", "지갑")),
    ("SHOES",            ("신발", "운동화", "구두", "슬리퍼", "샌들", "부츠")),
    ("WEAR",             ("의류", "티셔츠", "셔츠", "바지", "원피스", "아우터", "코트",
                          "양말", "레깅스", "속옷", "잠옷", "니트")),
    ("FASHION_ITEMS",    ("패션잡화", "패션소품", "모자", "벨트", "장갑", "머플러",
                          "스카프", "우산", "양산", "헤어액세서리", "헤어핀", "머리띠", "헤어밴드")),
    ("JEWELLERY",        ("주얼리", "귀금속", "반지", "목걸이", "귀걸이", "팔찌")),
    ("COSMETIC",         ("화장품", "스킨", "로션", "에센스", "세럼", "마스크팩", "클렌징")),
    ("SLEEPING_GEAR",    ("침구", "이불", "베개", "매트리스", "패드")),
    ("FURNITURE",        ("가구", "책상", "의자", "선반", "수납장", "옷장", "서랍")),
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


def resolve_notice_type(category_path: str) -> str:
    """
    카테고리 경로로 고시유형을 결정. 확실한 매치가 없으면 "ETC".

    맨 앞 대분류는 보지 않는다 — "취미/도서>정원/원예용품>화분"이 '도서'로, "가구/인테리어>
    침구단품>베개"가 '가구'로 걸렸다(2026-09 샘플 10건 중 3건 오분류). 대분류는 여러 품목을
    한데 묶은 이름이라 유형 판단 근거가 못 된다. 상품명도 보지 않는다 — "욕실 주방 현관
    발매트"처럼 쓰임새를 나열한 이름 때문에 발매트가 주방용품이 됐다.

    ETC로 떨어지는 건 실패가 아니라 "기타 재화"라는 유효한 유형이지만, 의류를 ETC로
    올리면 소재·치수·세탁방법 같은 고시 항목이 빠지므로 호출부에서 로그로 알려줄 것.
    """
    parts = [p for p in str(category_path or "").split(">") if p.strip()]
    haystack = ">".join(parts[1:] if len(parts) > 1 else parts)
    for notice_type, keywords in _TYPE_KEYWORDS:
        if any(k in haystack for k in keywords):
            return notice_type
    return "ETC"


def _field_value(field_name: str, product) -> Optional[str]:
    """항목 이름 → 도매매/상품 데이터에서 찾은 값. 못 찾으면 None(호출부가 폴백 처리)."""
    from .register import clean_manufacturer, clean_model, importer_from

    if field_name in ("itemName", "productName"):
        return product.name
    if field_name in CERT_FIELDS:
        return CERT_NOT_APPLICABLE
    if field_name == "warrantyPolicy":
        return WARRANTY_TEXT
    if field_name == "modelName":
        # 도매매 상품번호로 폴백하지 않는다 — 공급사 내부 관리번호가 고시에 그대로
        # 노출돼 위탁 소싱 구조가 드러났다(2026-09 발견). 모델명이 없는 상품이면
        # "해당없음"이 사실이다 — 상세페이지에도 없으니 "참조"는 거짓이 된다(2026-10).
        return clean_model(getattr(product, "model", ""), product.name) or CERT_NOT_APPLICABLE
    if field_name == "manufacturer":
        # 실제 회사명이 없으면 확인된 사실(제조국)만 적는다 — "~협력사"는 공급사를 드러낸다(2026-10).
        maker = clean_manufacturer(getattr(product, "manufacturer", ""))
        country = _producer_country(product)
        return maker or (f"{country} 제조" if country else None)
    if field_name == "importer":
        if not str(getattr(product, "origin_country", "") or "").startswith("수입산"):
            return CERT_NOT_APPLICABLE
        return importer_from(getattr(product, "manufacturer", "")) or SELLER_BUSINESS_NAME or None
    if field_name == "producer":
        return _producer_country(product) or None
    if field_name in ("afterServiceDirector", "customerServicePhoneNumber"):
        return CS_PHONE_NUMBER
    # 소재·크기 등 나머지는 "상세페이지 참조" — 상세 이미지에서 AI가 읽은 문구로 채워봤지만(2026-09)
    # 오독·광고 문구가 법정 표시 칸에 들어갈 위험이 더 커서 되돌렸다. 도매매가 주는 확실한 값만 쓴다.
    return None


def _producer_country(product) -> str:
    """제조국. 수입산은 도매매 원산지 표기("수입산_아시아_중국")의 마지막 조각, 국산은 "대한민국".
    예전엔 국산도 마지막 조각을 써서 "국산_서울특별시_종로구"가 제조국 "종로구"로 나갔다(2026-10).
    "상세정보별도표기"처럼 나라가 아닌 값은 제조국으로 쓰지 않는다."""
    raw = str(getattr(product, "origin_country", "") or "")
    if raw.startswith("국산"):
        return "대한민국"
    if raw.startswith(("수입산", "원양산")):
        return raw.split("_")[-1]
    return ""


def build_provided_notice(product, access_token: str, notice_type: str = "") -> dict:
    """
    productInfoProvidedNotice 전체를 생성. 유형에 정의된 String 항목을 빠짐없이 채운다.

    YearMonth/LocalDate/Boolean 항목은 값을 지어내면 허위 표시가 되므로 생략한다
    (제조연월·유통기한·수입신고 여부 등 — 도매매가 주지 않는 정보). 등록이 거부되면
    그때 해당 항목이 필수임이 확인되는 것이니 그 시점에 대응할 것.
    """
    ntype = notice_type or resolve_notice_type(_category_path(product, access_token))

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


def _category_path(product, access_token: str) -> str:
    """고시유형 판단에 쓸 카테고리 경로. 실제로 등록되는 네이버 카테고리가 우선이다 — 도매매
    분류는 공급사가 붙인 것이라 네이버와 어긋날 수 있다. 네이버 경로를 못 구하면 도매매 분류."""
    leaf_id = getattr(product, "leaf_category_id", "") or ""
    if leaf_id and access_token:
        try:
            from .category import describe_category
            path = describe_category(leaf_id, access_token)
            if path:
                return path
        except Exception:
            pass
    return getattr(product, "domemae_category", "") or getattr(product, "keyword", "")


def _node_name(notice_type: str) -> str:
    """KITCHEN_UTENSILS → kitchenUtensils, ETC → etc, WEAR → wear."""
    head, *rest = notice_type.lower().split("_")
    return head + "".join(w.capitalize() for w in rest)
