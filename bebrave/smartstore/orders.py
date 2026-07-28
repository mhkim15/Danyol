"""
네이버 커머스 API 주문 조회 및 발송처리(송장 입력).

엔드포인트 (네이버 커머스 API 공식 문서 기준):
  - 상태변경 주문 목록: GET  /external/v1/pay-order/seller/product-orders/last-changed-statuses
  - 주문 상세 조회    : POST /external/v1/pay-order/seller/product-orders/query
  - 발송처리(송장입력) : POST /external/v1/pay-order/seller/product-orders/dispatch

인증: auth.get_access_token()의 Bearer 토큰 (register.py와 동일 방식)

주의: 실제 크레덴셜(NAVER_COMMERCE_CLIENT_ID/SECRET)로 아직 검증되지 않음.
     최초 실행 시 --dry-run 또는 소량 데이터로 응답 구조를 확인할 것.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE_URL = "https://api.commerce.naver.com/external"

# 주요 택배사 코드 (네이버 커머스 API 공식 코드)
DELIVERY_COMPANY_CODES = {
    "CJ대한통운": "CJGLS",
    "우체국택배": "EPOST",
    "한진택배": "HANJIN",
    "롯데택배": "LOTTE",
    "로젠택배": "LOGEN",
}


@dataclass
class ProductOrder:
    product_order_id: str
    order_id: str
    product_name: str
    option_name: str = ""
    quantity: int = 1
    unit_price: int = 0
    status: str = ""              # PAYED(결제완료) / DELIVERING / DELIVERED 등
    orderer_name: str = ""
    orderer_tel: str = ""
    receiver_name: str = ""
    receiver_tel: str = ""
    receiver_address: str = ""     # 표시용 (address1+address2 합친 문자열)
    receiver_zipcode: str = ""
    receiver_address1: str = ""
    receiver_address2: str = ""
    delivery_memo: str = ""
    ordered_at: str = ""

    def summary(self) -> str:
        return (
            f"[주문 {self.product_order_id}] {self.product_name} {self.option_name}".strip()
            + f" x{self.quantity}  {self.unit_price:,}원  상태:{self.status}\n"
            f"  수령인: {self.receiver_name} ({self.receiver_tel})\n"
            f"  주소: {self.receiver_address}"
        )


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json;charset=UTF-8",
    }


_MAX_WINDOW_HOURS = 24  # last-changed-statuses API 제한 (실측 확인: 2026-07-12, "조회 날짜가 유효하지 않습니다" 오류로 확인됨)


def fetch_new_orders(
    access_token: str,
    hours: int = 24,
) -> List[ProductOrder]:
    """
    최근 N시간 내 결제완료(PAYED, 신규주문=발주 대기) 상태로 바뀐 주문 목록 조회.

    2단계로 동작:
      1) last-changed-statuses 로 최근 상태변경된 productOrderId 목록 취득
         (API가 한 번에 최대 24시간 범위만 허용하므로 N시간을 24시간 단위로 나눠 호출)
      2) query 로 각 주문의 상세정보(수령인/주소/상품명 등) 조회
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    now = datetime.now()
    ids: List[str] = []
    remaining = hours
    window_end = now

    while remaining > 0:
        window_hours = min(remaining, _MAX_WINDOW_HOURS)
        window_start = window_end - timedelta(hours=window_hours)

        resp = requests.get(
            f"{_BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
            headers=_headers(access_token),
            params={
                "lastChangedFrom": window_start.strftime("%Y-%m-%dT%H:%M:%S.000+09:00"),
                "lastChangedTo": window_end.strftime("%Y-%m-%dT%H:%M:%S.000+09:00"),
                "lastChangedType": "PAYED",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"주문 상태변경 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

        changed = resp.json()
        ids.extend(
            item.get("productOrderId", "")
            for item in changed.get("data", changed.get("lastChangeStatuses", []))
            if item.get("productOrderId")
        )

        window_end = window_start
        remaining -= window_hours

    ids = list(dict.fromkeys(ids))  # 중복 제거, 순서 유지
    if not ids:
        return []

    return fetch_order_detail(ids, access_token)


def fetch_order_detail(
    product_order_ids: List[str],
    access_token: str,
) -> List[ProductOrder]:
    """productOrderId 목록으로 주문 상세정보(수령인/주소 등) 조회."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.post(
        f"{_BASE_URL}/v1/pay-order/seller/product-orders/query",
        headers=_headers(access_token),
        json={"productOrderIds": product_order_ids},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"주문 상세 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    orders = []
    for item in data.get("data", []):
        content = item.get("productOrder", item)
        order = content.get("order", {})
        delivery = content.get("shippingAddress", {})
        orders.append(ProductOrder(
            product_order_id=str(content.get("productOrderId", "")),
            order_id=str(order.get("orderId", "")),
            product_name=content.get("productName", ""),
            option_name=content.get("productOption", ""),
            quantity=int(content.get("quantity", 1) or 1),
            unit_price=int(content.get("unitPrice", 0) or 0),
            status=content.get("productOrderStatus", ""),
            orderer_name=order.get("ordererName", ""),
            orderer_tel=order.get("ordererTel", ""),
            receiver_name=delivery.get("name", ""),
            receiver_tel=delivery.get("tel1", ""),
            receiver_address=(delivery.get("baseAddress", "") + " " + delivery.get("detailAddress", "")).strip(),
            receiver_zipcode=delivery.get("zipCode", ""),
            receiver_address1=delivery.get("baseAddress", ""),
            receiver_address2=delivery.get("detailAddress", ""),
            delivery_memo=delivery.get("deliveryMemo", ""),
            ordered_at=order.get("orderDate", ""),
        ))
    return orders


def dispatch_order(
    product_order_id: str,
    tracking_number: str,
    delivery_company: str,
    access_token: str,
) -> bool:
    """
    송장번호 입력 → 발송처리. 스마트스토어 주문 상태가 '배송중'으로 바뀜.

    Args:
        delivery_company: DELIVERY_COMPANY_CODES 의 키(한글명) 또는 코드값 그대로
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    code = DELIVERY_COMPANY_CODES.get(delivery_company, delivery_company)

    body = {
        "dispatchProductOrders": [{
            "productOrderId": product_order_id,
            "deliveryMethod": "DELIVERY",
            "deliveryCompanyCode": code,
            "trackingNumber": tracking_number,
        }]
    }
    resp = requests.post(
        f"{_BASE_URL}/v1/pay-order/seller/product-orders/dispatch",
        headers=_headers(access_token),
        json=body,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"발송처리 실패 [{resp.status_code}]: {resp.text[:300]}")
    return True
