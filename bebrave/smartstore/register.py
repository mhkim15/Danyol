"""
네이버 커머스 API 상품 등록.

API: POST https://api.commerce.naver.com/external/v2/products
인증: Bearer 토큰 (auth.py에서 발급)

요청 바디는 최상위가 아니라 originProduct/smartstoreChannelProduct로 감싸야 함
(2026-07-12 실전 테스트로 확인 — 최초 구현은 필드를 최상위에 둬서 400 오류 발생했음).
"""
import os
import re
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from ..config import (
    SHIPPING_FEE, FREE_SHIPPING_THRESHOLD, RETURN_DELIVERY_FEE, EXCHANGE_DELIVERY_FEE,
)
from .models import StoreProduct
from .notice import CS_PHONE_NUMBER, DUMMY_CS_PHONE_NUMBER, build_provided_notice

_BASE_URL = "https://api.commerce.naver.com/external"


def register_product(
    product: StoreProduct,
    access_token: str,
    status: str = "SUSPENSION",
) -> str:
    """
    스마트스토어에 상품 등록.

    Args:
        product     : StoreProduct 데이터
        access_token: 커머스 API Bearer 토큰
        status      : 'SUSPENSION'(판매중지, 기본) or 'ON'(판매중)
                      처음엔 SUSPENSION으로 등록 후 수동 확인 권장

    Returns:
        등록된 상품 ID (originProductNo)
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    body = build_request_body(product, status=status, access_token=access_token)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json;charset=UTF-8",
    }

    resp = requests.post(
        f"{_BASE_URL}/v2/products",
        headers=headers,
        json=body,
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"상품 등록 실패 [{resp.status_code}]: {resp.text[:300]}"
        )

    result = resp.json()
    product_id = str(
        result.get("originProductNo",
        result.get("productNo",
        result.get("id", "")))
    )

    # POST 생성 시 statusType=SUSPENSION을 보내도 네이버 쪽이 무조건 SALE로 생성하는 것을
    # 실전 테스트로 확인함(2026-07-12) — SUSPENSION을 요청했다면 즉시 PUT으로 강제 전환.
    if status == "SUSPENSION" and product_id:
        _force_suspend(product_id, access_token)

    return product_id


def update_registered_product(product_id: str, access_token: str, mutate) -> None:
    """
    등록된 상품을 조회 → mutate(body)로 필요한 부분만 고침 → 전체를 다시 전송.

    네이버 상품 수정 API는 부분 갱신을 지원하지 않는다. 요청에 포함하지 않은 항목은
    삭제되므로, 바꿀 값만 보내면 나머지 정보가 통째로 날아간다. 그래서 반드시 현재
    상태를 조회해서 그 위에 수정을 얹어 보내야 한다 (2026-08-10 공식 문서 확인).

    mutate는 조회한 요청 바디(dict)를 받아 제자리에서 고치는 함수.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    current = fetch_registered_product(product_id, access_token)
    mutate(current)

    resp = requests.put(
        f"{_BASE_URL}/v2/products/origin-products/{product_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json;charset=UTF-8",
        },
        json=current,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"상품 수정 실패 [{resp.status_code}]: {resp.text[:300]}")


def _set_suspended(body: dict) -> None:
    body["originProduct"]["statusType"] = "SUSPENSION"
    if "smartstoreChannelProduct" in body:
        body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "SUSPENSION"


def _force_suspend(product_id: str, access_token: str) -> None:
    try:
        update_registered_product(product_id, access_token, _set_suspended)
    except RuntimeError as e:
        raise RuntimeError(
            f"판매중지 강제전환 실패: {e} "
            f"— 상품 ID {product_id}가 SALE 상태로 남아있을 수 있으니 스마트스토어센터에서 직접 확인 필요"
        )


def build_request_body(
    product: StoreProduct,
    status: str = "SUSPENSION",
    access_token: str = "",
    strict: bool = True,
) -> dict:
    """StoreProduct → 커머스 API v2 요청 바디 변환 (originProduct/smartstoreChannelProduct 구조).

    strict=False는 실제 등록에는 절대 쓰지 않는다 — "등록 항목 점검" 패널이 등록 전에
    전체 항목을 보여주기 위한 용도다. 원산지 코드나 A/S 연락처가 아직 안 채워졌어도
    막지 않고 빈 값/더미값 그대로 바디를 만들어 돌려주면, field_audit이 그 자리에서
    "비어 있음"/"더미 의심"으로 정확히 잡아준다 — 예전엔 이 두 조건 중 하나만 안
    맞아도 점검 패널 자체가 안 뜨고 사유 한 줄만 보였다(2026-09 발견)."""
    # A/S 연락처가 더미면 원산지 코드와 같은 방식으로 등록을 막는다 — 예전엔 경고만
    # 찍고 그대로 등록을 진행해, 실제 연락처 없이 상품이 나가고 있었다(2026-09).
    if strict and CS_PHONE_NUMBER == DUMMY_CS_PHONE_NUMBER:
        raise ValueError(
            "A/S 연락처 미설정 — .env의 CS_PHONE_NUMBER를 실제 번호로 채워야 등록할 수 있습니다 "
            "(더미 번호 010-0000-0000로 등록되는 걸 막기 위함)"
        )

    detail_attribute = {
        "afterServiceInfo": {
            # 전화번호 필드 자체는 네이버 스펙상 필수라 비워둘 수 없다 — 다만 안내
            # 문구는 통화 대신 스마트스토어 톡톡으로 유도한다(2026-09, 1인 운영이라
            # 전화 응대 대신 톡톡으로 문의 채널을 일원화하기로 함).
            "afterServiceTelephoneNumber": CS_PHONE_NUMBER,
            "afterServiceGuideContent": "구매 후 문의사항은 스마트스토어 톡톡으로 문의해 주세요.",
        },
        "originAreaInfo": _build_origin_area_info(product, strict=strict),
        # 검색어 태그 — code 없이 text만 등록 (네이버 공식 가이드상 code 생략 가능,
        # code를 쓰려면 별도 '추천 태그 검색' API로 조회해야 하나 엔드포인트 미확인)
        "seoInfo": {"sellerTags": [{"text": t} for t in product.tags]},
        "minorPurchasable": True,
        # 상품정보제공고시 — 카테고리에 맞는 유형을 골라 그 유형의 항목을 빠짐없이 채운다.
        # 예전엔 전 상품을 ETC로 고정하고 항목도 일부만 채우고 있었음 (notice.py 참고).
        "productInfoProvidedNotice": build_provided_notice(product, access_token),
    }
    # 판매자상품코드에 도매매 상품번호를 심는다 — 등록/발주 양쪽에서 같은 값으로 상품을
    # 찾을 수 있는 유일한 확실한 키다. 지금까지는 이게 없어 발주 자동매칭이 상품ID·
    # 이름 추론에 의존했다(2026-09).
    if product.domemae_goods_no:
        detail_attribute["sellerCodeInfo"] = {"sellerManagementCode": product.domemae_goods_no}
    # 제조사/모델 — 값은 이미 갖고 있는데(도매매 detail.manufacturer/.model) 지금까지
    # 고시 블록에만 쓰고 정작 가격비교 매칭에 쓰이는 전용 필드엔 안 넣고 있었다(2026-09).
    # 브랜드는 도매매가 별도로 주지 않아 지어내지 않고 비워둔다.
    # 도매매 값은 "~협력사"·"중국(OEM)"·상품명 복사본이 많아 정제한 값만 쓴다(2026-09).
    manufacturer = clean_manufacturer(product.manufacturer)
    if manufacturer:
        detail_attribute["manufacturerName"] = manufacturer
    model = clean_model(product.model, product.name)
    if model:
        detail_attribute["modelName"] = model
    # 속성(색상/소재/사이즈 등) — 네이버쇼핑 SEO 가이드가 "필터 결과 최상단 노출"의
    # 조건으로 명시하는 필드인데 지금까지 아예 안 보내고 있었다(2026-09 발견).
    # attributes.match_attributes()가 상품명·옵션명에 실제로 등장하는 값만 채택해두므로
    # 빈 리스트면 그냥 키를 생략한다(제조사/모델과 같은 방식).
    if product.attributes:
        detail_attribute["productAttributes"] = product.attributes

    # CLI(main.py --on-sale)는 "ON"을, 그 외 호출부는 "SALE"을 판매중 신호로 쓴다 —
    # 둘 다 같은 뜻인데 originProduct.statusType 유효값은 SALE/SUSPENSION뿐이라
    # "ON"을 그대로 보내면 원상품 상태값이 깨진다(2026-09 발견, CLI --on-sale
    # 경로만 영향 — 웹은 항상 SUSPENSION으로 등록 후 수동 전환이라 무관했음).
    is_on_sale = status in ("SALE", "ON")
    origin_product = {
        "statusType": "SALE" if is_on_sale else "SUSPENSION",
        "saleType": "NEW",
        "leafCategoryId": product.leaf_category_id,
        "name": product.name,
        "detailContent": product.detail_content,
        "images": {
            "representativeImage": {"url": product.representative_image},
            "optionalImages": [{"url": u} for u in product.optional_images if u],
        },
        "salePrice": product.sale_price,
        "stockQuantity": product.stock_quantity,
        "deliveryInfo": _build_delivery_info(),
        "detailAttribute": detail_attribute,
    }
    if sellable_options(product.options):
        # 네이버 스펙상 optionInfo는 detailAttribute 아래여야 한다 — 예전엔 originProduct
        # 최상위에 붙여서 옵션이 통째로 무시될 가능성이 있었다(2026-09 발견).
        detail_attribute["optionInfo"] = _build_option_info(product.option_group_name, product.options)

    smartstore_channel_product = {
        "naverShoppingRegistration": True,
        "channelProductDisplayStatusType": "ON" if is_on_sale else "SUSPENSION",
    }
    result = {
        "originProduct": origin_product,
        "smartstoreChannelProduct": smartstore_channel_product,
    }
    if product.discount_rate:
        # 즉시할인 — 목록에서 할인가·할인율 뱃지가 붙어 클릭률에 직접 영향을 준다.
        # 정률(PERCENT) 할인만 지원. 실전 등록으로 구조가 검증되진 않았으니(optionInfo와
        # 같은 이유로) 처음 쓸 때는 반드시 SUSPENSION으로 1건 등록해 센터에서 할인이
        # 정상 반영됐는지 눈으로 확인할 것(2026-09).
        result["originProduct"]["customerBenefit"] = {
            "immediateDiscountPolicy": {
                "discountMethod": {
                    "value": round(product.discount_rate * 100, 1),
                    "unitType": "PERCENT",
                },
            },
        }
    if product.field_overrides:
        # "등록 항목 점검" 패널에서 직접 고친 값을 최종 바디에 덮어쓴다 — 점검 화면이
        # 문제를 보여주기만 하고 고칠 방법이 없다는 지적으로 추가(2026-09). 위에서 만든
        # 값을 전부 무시하고 사용자가 입력한 값을 최종 우선시킨다.
        _apply_field_overrides(result["originProduct"], product.field_overrides)
    return result


def _apply_field_overrides(node: dict, overrides: dict) -> None:
    """overrides의 key는 "detailAttribute.brandName" 같은 점(.) 경로 — 중간 노드가
    없으면 만들면서 마지막 키에 값을 꽂는다. 빈 문자열 입력은 "안 고침"으로 취급해
    건너뛴다(사용자가 아무것도 안 적은 칸이 굳이 기존 값을 지우지 않도록)."""
    for path, value in overrides.items():
        if value is None or str(value).strip() == "":
            continue
        parts = path.split(".")
        target = node
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value


def _build_option_info(group_name: str, options: list) -> dict:
    """
    옵션(색상/사이즈 등 단일 축) → 커머스 API v2 optionCombinations 구조.
    주의: 실전 등록으로 검증되지 않은 구조 — 처음 쓸 때는 반드시 --dry-run으로
    요청 바디를 먼저 확인하고, SUSPENSION 상태로 1건 등록해 스마트스토어센터에서
    옵션이 정상 반영됐는지 눈으로 확인할 것.
    옵션별 추가금액은 도매매 원가 차액(supPrice)을 그대로 씀 — 마진율은 기본
    판매가에만 반영되고 옵션 추가금엔 마진이 안 붙는 단순화.
    """
    return {
        "optionCombinationSortType": "CREATE",
        "useStockManagement": True,
        "optionCombinationGroupNames": {"optionGroupName1": clean_option_group_name(group_name)},
        "optionCombinations": [
            {
                "optionName1": o["name"],
                "stockQuantity": min(o.get("stock", 0), 9999),
                "price": o.get("extra_price", 0),
                "usable": True,
            }
            for o in sellable_options(options)
        ],
    }


def sellable_options(options: list) -> list:
    """구매자에게 보여도 되는 옵션만. 도매매는 품절 옵션을 지우지 않고 "품절(선택X)"처럼
    이름만 바꿔 재고를 남겨두기도 한다 — 그대로 올리면 주문은 받는데 발주를 못 한다(2026-10
    실측: 일회용베개커버 "품절(선택X)" 재고 10). 이름에 품절이 있거나 재고가 0이면 뺀다.
    StoreProduct.options 자체는 그대로 둔다 — 발주 매칭이 옵션코드를 찾는 데 쓴다."""
    return [o for o in options if "품절" not in str(o.get("name", "")) and int(o.get("stock", 0) or 0) > 0]


def clean_option_group_name(name: str) -> str:
    """"선택하세요"처럼 안내문을 옵션 제목으로 쓴 경우 "옵션"으로 바꾼다(2026-10)."""
    n = str(name or "").strip()
    return "옵션" if not n or "선택" in n else n


def _build_delivery_info() -> dict:
    """기본 배송 정보 (도매매 배송대행 기준). 배송비 상수는 마진 계산(config.py)과 값이
    갈리지 않도록 여기서 리터럴로 다시 정의하지 않고 그대로 가져다 쓴다(2026-09)."""
    return {
        "deliveryType": "DELIVERY",
        "deliveryAttributeType": "NORMAL",
        "deliveryCompany": "CJGLS",
        "deliveryFee": {
            "deliveryFeeType": "CONDITIONAL_FREE",
            "deliveryFeePayType": "PREPAID",
            "baseFee": SHIPPING_FEE,
            "freeConditionalAmount": FREE_SHIPPING_THRESHOLD,
        },
        "claimDeliveryInfo": {
            "returnDeliveryFee": RETURN_DELIVERY_FEE,
            "exchangeDeliveryFee": EXCHANGE_DELIVERY_FEE,
        },
        "installation": False,
    }


def _build_origin_area_info(product: StoreProduct, strict: bool = True) -> dict:
    """
    원산지 정보 생성. pipeline이 origin.resolve_origin_code로 미리 찾아둔 코드를 쓴다.

    이전 버전은 originAreaCode를 "03"으로 고정하고 주석에 "03(국산)"이라 적어뒀는데,
    실제 코드표상 03은 "상세설명에 표시"였다(2026-08-10 확인). 국산은 00, 수입산은
    02 하위 코드. 코드가 비어 있으면 원산지를 특정하지 못한 것이므로 등록을 막는다.
    strict=False면(등록 항목 점검용) 막지 않고 빈 코드 그대로 반환한다.
    """
    if not product.origin_code:
        if not strict:
            return {"originAreaCode": "", "content": ""}
        raise ValueError(
            f"원산지 코드 미확정 (도매매 원본값: '{product.origin_country or '(미표기)'}') — "
            "잘못된 원산지 표시를 막기 위해 등록 금지"
        )
    info = {"originAreaCode": product.origin_code, "content": ""}
    # 수입산(02 계열)에만 수입사를 채운다 — 도매매가 수입자를 밝혔으면 그 값, 아니면 판매자
    # 상호(2026-10 결정: 실제 수입자는 공급사도 모르는 경우가 대부분이고, 제조·수입자를 특정
    # 못 하면 어차피 판매자가 책임지는 구조라 판매자 상호를 쓴다).
    if product.origin_code.startswith("02"):
        from .notice import SELLER_BUSINESS_NAME
        importer = importer_from(product.manufacturer) or SELLER_BUSINESS_NAME
        if not importer and strict:
            raise ValueError("수입자 미설정 — .env의 SELLER_BUSINESS_NAME에 판매자 상호를 넣어야 수입산 상품을 등록할 수 있습니다")
        if importer:
            info["importer"] = importer
    return info


# 도매매가 "값 없음"을 뜻하는 문자열로 채워 보내는 경우가 있어 실제 값과 구분한다
_PLACEHOLDER_VALUES = {"해당없음", "없음", "미상", "-", "n/a", "na"}


def _clean(value: str) -> str:
    """도매매 필드에서 '해당없음' 같은 자리표시자를 걸러낸 실제 값. 없으면 빈 문자열.
    "상세페이지 참조"·"상세정보 별도표기"류도 값이 아니다 — 제조사 칸에 그대로 들어가
    제조사 이름이 "상세페이지 참조"로 등록될 뻔했다(2026-10)."""
    v = str(value or "").strip()
    if v.lower() in _PLACEHOLDER_VALUES:
        return ""
    if "상세" in v and any(w in v for w in ("참조", "표기", "표시", "별도")):
        return ""
    return v


# 도매매 제조사 칸은 실제 제조사명이 아닌 경우가 대부분이었다 — 샘플 10건 중 7건이
# "ablecompany협력사", "업체협력사", "중국(OEM)" 같은 값(2026-09-30 조회). 이게 제조사·
# 고시 제조자·수입자 칸에 그대로 나가 "수입자: 중국(OEM)"처럼 표시되고 위탁 구조까지 드러났다.
# 틀린 값보다 빈 값이 낫다 — 비우면 고시는 "상세페이지 참조", 제조사 칸은 생략된다.
_MAKER_JUNK = ("협력사", "협력업체", "업체협력", "공급사", "위탁")
_IMPORTER_PREFIXES = ("수입판매원", "수입원", "수입사", "수입자")
_MAKER_PREFIXES = _IMPORTER_PREFIXES + ("제조판매원", "제조원", "제조사", "판매원")
_COUNTRY_ONLY = {"중국", "한국", "국산", "국내", "베트남", "일본", "대만", "인도", "태국", "파키스탄",
                 "인도네시아", "미국", "수입산", "해외"}


def _strip_maker_prefix(value: str) -> tuple:
    """(접두어 뗀 값, 수입자 표기였는지). "수입판매원 (주)미래" → ("(주)미래", True)"""
    v = value.strip()
    for pre in _MAKER_PREFIXES:
        if v.startswith(pre):
            return v[len(pre):].lstrip(" :：-").strip(), pre in _IMPORTER_PREFIXES
    return v, False


def clean_manufacturer(value: str) -> str:
    """제조사로 써도 되는 값만 남긴다. "셀링온(OEM)" → "셀링온", "중국(OEM)"·"~협력사" → ""."""
    v, _ = _strip_maker_prefix(_clean(value))
    if not v or any(j in v for j in _MAKER_JUNK):
        return ""
    v = re.sub(r"\s*[\(\[]\s*OEM\s*[\)\]]\s*", "", v, flags=re.I).strip()
    # \b는 한글 바로 뒤의 OEM을 못 잡는다("중국OEM" — 한글도 단어 문자라 경계가 없음, 2026-10)
    v = re.sub(r"(?<![A-Za-z])OEM(?![A-Za-z])", "", v, flags=re.I).strip()
    return "" if not v or v in _COUNTRY_ONLY else v


def importer_from(value: str) -> str:
    """도매매 제조사 칸이 수입자라고 스스로 밝힌 경우("수입판매원 ~")에만 수입자로 쓴다.
    예전엔 수입산이면 제조사 칸을 무조건 수입자로 넣어 "수입자: 셀링온(OEM)"이 나갔다 —
    제조사를 수입자로 갈음할 근거가 없다. 못 찾으면 빈 값(판매자 상호를 넣는 건 사람이 정할 일)."""
    _, is_importer = _strip_maker_prefix(_clean(value))
    return clean_manufacturer(value) if is_importer else ""


def clean_model(value: str, product_name: str = "") -> str:
    """모델명으로 써도 되는 값만 남긴다. 도매매 모델명 칸에 상품명을 그대로 베끼거나("큐티클 니퍼
    니퍼 큐티클관리니퍼") 일반명사("샴푸 브러쉬", "요가링(하드타입)")를 넣은 경우가 샘플 10건 중
    5건이었다(2026-09-30). 모델명은 가격비교에서 같은 상품을 묶는 기준이라 엉뚱한 값이 해롭다.
    실제 모델명은 거의 항상 영문·숫자를 포함하므로, 한글뿐인 값과 상품명의 조각은 버린다."""
    v = _clean(value).strip("()[] ")
    if not v or not re.search(r"[A-Za-z0-9]", v):
        return ""
    # 숫자뿐인 값("0671" — 원본 상품명 "[ABC0671]"의 공급사 코드 조각)과 공급사 관리코드
    # 형태는 모델명이 아니다 — 가격비교 묶음을 오염시키고 소싱처를 드러낸다(2026-10).
    if v.isdigit() or is_supplier_code(v):
        return ""
    tokens = [t for t in re.split(r"[\s\(\)\[\]/,·]+", v) if t]
    name = str(product_name or "")
    if name and tokens and all(t in name for t in tokens):
        return ""
    return v


def is_supplier_code(model: str) -> bool:
    """확실한 공급사 관리코드 — 등록에서 아예 뺀다. 도매매 관리코드는 영문 접두어 + "WB" +
    숫자 7자리 꼴이 많았고(DSJJWB7001360·megaWB5072450·TJWB5034271, 2026-10 샘플 14건 중 5건),
    구분자 없이 영문 6자 이상 + 숫자 6자리 이상 붙은 값도 실제 제품 모델명에선 보기 드물다."""
    v = str(model or "").strip()
    if re.fullmatch(r"[A-Za-z]*WB\d{6,}", v, flags=re.I):
        return True
    return bool(re.fullmatch(r"[A-Za-z]{6,}\d{6,}", v))


def looks_like_supplier_code(model: str) -> bool:
    """"dwa1168", "HJ5169-366"처럼 영문+숫자 코드만으로 된 모델명 — 진짜 모델명일 수도 있어
    지우진 않지만, 공급사 관리코드면 소싱처가 추적되므로 등록 항목 점검에서 확인을 요청한다."""
    v = str(model or "").strip()
    return is_supplier_code(v) or bool(re.fullmatch(r"[A-Za-z]{1,5}[-_]?\d{3,}([-_]\d+)*", v))


def fetch_registered_product(product_id: str, access_token: str) -> dict:
    """등록된 상품 정보 조회 (등록 결과 확인용)."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"{_BASE_URL}/v2/products/origin-products/{product_id}",
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _demo() -> None:
    """실행 가능한 자체 점검 — 등록 항목 점검 패널에서 고친 값이 실제 바디에 정확히
    꽂히는지, 빈 입력은 기존 값을 안 지우는지 확인 (네트워크 호출 없음, 2026-09)."""
    body = {"originProduct": {"detailAttribute": {"brandName": ""}}}
    _apply_field_overrides(body["originProduct"], {
        "detailAttribute.brandName": "테스트브랜드",
        "detailAttribute.afterServiceInfo.afterServiceTelephoneNumber": "010-1234-5678",
        "detailAttribute.manufacturerName": "   ",  # 빈 칸(공백만) — 반영되면 안 됨
    })
    assert body["originProduct"]["detailAttribute"]["brandName"] == "테스트브랜드"
    assert body["originProduct"]["detailAttribute"]["afterServiceInfo"]["afterServiceTelephoneNumber"] == "010-1234-5678"
    assert "manufacturerName" not in body["originProduct"]["detailAttribute"], "빈 입력인데 덮어씀"
    print("register._apply_field_overrides self-check OK")


if __name__ == "__main__":
    _demo()
