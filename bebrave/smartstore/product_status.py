"""
등록 상품 전체의 판매상태 일괄 조회.

기존 registered_status()(웹앱)는 상품 하나씩 개별 조회 API(GET
/v2/products/origin-products/{no})를 호출해야 해서, 상품이 늘어날수록
"실시간 상태" 버튼을 하나하나 눌러야 했다. 목록 조회 API를 쓰면 한 번의
호출로 전체 상품의 현재 판매상태를 받아온다 — 내가 모르는 사이 품절·
판매중지된 상품을 스토어 전체 스캔으로 잡아내는 용도.

API: POST https://api.commerce.naver.com/external/v1/products/search
(GET 아님 — 쿼리 파라미터가 아니라 JSON 바디로 보내야 한다는 게 GitHub
commerce-api-naver/commerce-api 여러 스레드에서 공통으로 지적된 함정이다.
2026-08 확인, apicenter 직접 접근은 차단되어 실호출로 최종 검증은 못 함.)

바디 예시: {"searchKeywordType": "CHANNEL_PRODUCT_NO", "productStatusTypes": [...],
            "page": 1, "size": 100}
응답 필드명은 문서 미확인 — register.py/inquiries.py와 같은 방식으로 방어적 파싱.
"""
import os
from dataclasses import dataclass
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE_URL = "https://api.commerce.naver.com/external"

STATUS_SALE = "SALE"
STATUS_SUSPENSION = "SUSPENSION"
STATUS_OUTOFSTOCK = "OUTOFSTOCK"


def _get(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


@dataclass
class ProductStatus:
    product_id: str
    name: str
    status_type: str
    stock_quantity: int = 0
    modified_date: str = ""


def fetch_product_statuses(access_token: str, size: int = 100) -> List[ProductStatus]:
    """등록 상품 전체(최대 size건)의 현재 판매상태. 스토어 상품이 size를 넘으면
    다음 페이지가 있다는 뜻이므로 결과 길이로 짐작할 수 있다(페이지네이션 응답
    필드가 문서에 없어 total 카운트를 신뢰 있게 못 읽는다 — 우선 1페이지만 지원)."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.post(
        f"{_BASE_URL}/v1/products/search",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"page": 1, "size": max(10, min(size, 200))},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"상품목록 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    items = data.get("contents") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("content", [])

    results = []
    for it in items:
        # 채널상품/원상품 두 레벨로 응답이 올 수 있어 후보 키를 여러 개 시도한다.
        product = it.get("channelProducts", [{}])[0] if it.get("channelProducts") else it
        results.append(ProductStatus(
            product_id=str(_get(it, "originProductNo", "channelProductNo", "productNo")
                           or _get(product, "channelProductNo", "originProductNo")),
            name=_get(it, "name", "originProductName") or _get(product, "name", "channelProductName"),
            status_type=_get(product, "statusType", "channelProductDisplayStatusType")
                        or _get(it, "statusType", default=""),
            stock_quantity=int(_get(it, "stockQuantity", default=0) or _get(product, "stockQuantity", default=0) or 0),
            modified_date=_get(it, "modifiedDate", "regDate", default=""),
        ))
    return results


def find_status_mismatches(local_products: list, live_statuses: List[ProductStatus]) -> list:
    """로컬 등록 원장(registered_products.json)에는 있는데 실제 스토어에서
    품절·판매중지 상태인 상품을 찾는다 — 로컬 원장은 상태 필드 자체를 저장하지
    않으므로(등록 당시 스냅샷) '몰랐던 상태 변화'를 잡아내는 용도."""
    live_by_id = {s.product_id: s for s in live_statuses}
    mismatches = []
    for p in local_products:
        pid = str(p.get("naver_product_id", ""))
        if not pid:
            continue
        live = live_by_id.get(pid)
        if live is None:
            continue
        if live.status_type and live.status_type != STATUS_SALE:
            mismatches.append({
                "naver_product_id": pid,
                "name": p.get("name", ""),
                "live_status": live.status_type,
                "stock_quantity": live.stock_quantity,
            })
    return mismatches


def _demo() -> None:
    """실행 가능한 자체 점검 — 불일치 탐지 로직만 검증 (네트워크 호출 없음)."""
    local = [
        {"naver_product_id": "1", "name": "정상판매중"},
        {"naver_product_id": "2", "name": "몰래품절"},
        {"naver_product_id": "3", "name": "로컬에만있음"},
    ]
    live = [
        ProductStatus("1", "정상판매중", STATUS_SALE),
        ProductStatus("2", "몰래품절", STATUS_OUTOFSTOCK, stock_quantity=0),
    ]
    mismatches = find_status_mismatches(local, live)
    assert len(mismatches) == 1 and mismatches[0]["naver_product_id"] == "2", "불일치 탐지 실패"
    print("product_status self-check OK")


if __name__ == "__main__":
    _demo()
