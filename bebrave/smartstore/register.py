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

    body = build_request_body(product, status=status)
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


def _force_suspend(product_id: str, access_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json;charset=UTF-8",
    }
    current = fetch_registered_product(product_id, access_token)
    current["originProduct"]["statusType"] = "SUSPENSION"
    if "smartstoreChannelProduct" in current:
        current["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "SUSPENSION"

    resp = requests.put(
        f"{_BASE_URL}/v2/products/origin-products/{product_id}",
        headers=headers,
        json=current,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"판매중지 강제전환 실패 [{resp.status_code}]: {resp.text[:300]} "
            f"— 상품 ID {product_id}가 SALE 상태로 남아있을 수 있으니 스마트스토어센터에서 직접 확인 필요"
        )


def build_request_body(product: StoreProduct, status: str = "SUSPENSION") -> dict:
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
                "afterServiceTelephoneNumber": "010-0000-0000",
                "afterServiceGuideContent": "구매 후 문의사항은 고객센터로 연락 바랍니다.",
            },
            "originAreaInfo": {"originAreaCode": "03", "importer": "", "content": ""},
            # 검색어 태그 — code 없이 text만 등록 (네이버 공식 가이드상 code 생략 가능,
            # code를 쓰려면 별도 '추천 태그 검색' API로 조회해야 하나 엔드포인트 미확인)
            "seoInfo": {"sellerTags": [{"text": t} for t in product.tags]},
            "minorPurchasable": True,
            "productInfoProvidedNotice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "itemName": product.name,
                    # 도매매 공급사 ID(예: seoul7rsoe)는 실제 제조사명이 아니므로 넣지 않음
                    # (2026-07-12 SEO 가이드 대조 중 발견된 버그 — 이전엔 supplier를 그대로 넣었음)
                    "modelName": product.domemae_goods_no or "상세페이지 참조",
                    "manufacturer": "상세페이지 참조",
                    "customerServicePhoneNumber": "010-0000-0000",
                },
            },
        },
    }
    smartstore_channel_product = {
        "naverShoppingRegistration": True,
        "channelProductDisplayStatusType": "ON" if status == "SALE" else "SUSPENSION",
    }
    return {
        "originProduct": origin_product,
        "smartstoreChannelProduct": smartstore_channel_product,
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
