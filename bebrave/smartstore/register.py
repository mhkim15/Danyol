"""
네이버 커머스 API 상품 등록.

API: POST https://api.commerce.naver.com/external/v2/products
인증: Bearer 토큰 (auth.py에서 발급)

요청 바디는 최상위가 아니라 originProduct/smartstoreChannelProduct로 감싸야 함
(2026-07-12 실전 테스트로 확인 — 최초 구현은 필드를 최상위에 둬서 400 오류 발생했음).
"""
import os
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from .models import StoreProduct
from .notice import CS_PHONE_NUMBER, build_provided_notice

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
) -> dict:
    """StoreProduct → 커머스 API v2 요청 바디 변환 (originProduct/smartstoreChannelProduct 구조)."""
    origin_product = {
        "statusType": status,
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
        "detailAttribute": {
            "afterServiceInfo": {
                "afterServiceTelephoneNumber": CS_PHONE_NUMBER,
                "afterServiceGuideContent": "구매 후 문의사항은 고객센터로 연락 바랍니다.",
            },
            "originAreaInfo": _build_origin_area_info(product),
            # 검색어 태그 — code 없이 text만 등록 (네이버 공식 가이드상 code 생략 가능,
            # code를 쓰려면 별도 '추천 태그 검색' API로 조회해야 하나 엔드포인트 미확인)
            "seoInfo": {"sellerTags": [{"text": t} for t in product.tags]},
            "minorPurchasable": True,
            # 상품정보제공고시 — 카테고리에 맞는 유형을 골라 그 유형의 항목을 빠짐없이 채운다.
            # 예전엔 전 상품을 ETC로 고정하고 항목도 일부만 채우고 있었음 (notice.py 참고).
            "productInfoProvidedNotice": build_provided_notice(product, access_token),
        },
    }
    if product.options:
        origin_product["optionInfo"] = _build_option_info(product.option_group_name, product.options)

    smartstore_channel_product = {
        "naverShoppingRegistration": True,
        "channelProductDisplayStatusType": "ON" if status == "SALE" else "SUSPENSION",
    }
    return {
        "originProduct": origin_product,
        "smartstoreChannelProduct": smartstore_channel_product,
    }


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
        "optionCombinationGroupNames": {"optionGroupName1": group_name},
        "optionCombinations": [
            {
                "optionName1": o["name"],
                "stockQuantity": min(o.get("stock", 0), 9999),
                "price": o.get("extra_price", 0),
                "usable": True,
            }
            for o in options
        ],
    }


def _build_delivery_info() -> dict:
    """기본 배송 정보 (도매매 배송대행 기준)."""
    return {
        "deliveryType": "DELIVERY",
        "deliveryAttributeType": "NORMAL",
        "deliveryCompany": "CJGLS",
        "deliveryFee": {
            "deliveryFeeType": "CONDITIONAL_FREE",
            "deliveryFeePayType": "PREPAID",
            "baseFee": 3000,
            "freeConditionalAmount": 30000,
        },
        "claimDeliveryInfo": {
            "returnDeliveryFee": 3000,
            "exchangeDeliveryFee": 6000,
        },
        "installation": False,
    }


def _build_origin_area_info(product: StoreProduct) -> dict:
    """
    원산지 정보 생성. pipeline이 origin.resolve_origin_code로 미리 찾아둔 코드를 쓴다.

    이전 버전은 originAreaCode를 "03"으로 고정하고 주석에 "03(국산)"이라 적어뒀는데,
    실제 코드표상 03은 "상세설명에 표시"였다(2026-08-10 확인). 국산은 00, 수입산은
    02 하위 코드. 코드가 비어 있으면 원산지를 특정하지 못한 것이므로 등록을 막는다.
    """
    if not product.origin_code:
        raise ValueError(
            f"원산지 코드 미확정 (도매매 원본값: '{product.origin_country or '(미표기)'}') — "
            "잘못된 원산지 표시를 막기 위해 등록 금지"
        )
    info = {"originAreaCode": product.origin_code, "content": ""}
    # 수입산(02 계열)에만 수입사를 채운다 — 도매매가 주는 제조사를 수입사로 갈음
    if product.origin_code.startswith("02") and _clean(product.manufacturer):
        info["importer"] = _clean(product.manufacturer)
    return info


# 도매매가 "값 없음"을 뜻하는 문자열로 채워 보내는 경우가 있어 실제 값과 구분한다
_PLACEHOLDER_VALUES = {"해당없음", "없음", "미상", "-", "n/a", "na"}


def _clean(value: str) -> str:
    """도매매 필드에서 '해당없음' 같은 자리표시자를 걸러낸 실제 값. 없으면 빈 문자열."""
    v = str(value or "").strip()
    return "" if v.lower() in _PLACEHOLDER_VALUES else v


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
