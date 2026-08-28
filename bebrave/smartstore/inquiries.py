"""
스마트스토어 상품문의(Q&A) 조회.

API: GET https://api.commerce.naver.com/external/v1/pay-user/inquiries
공식 문서 파라미터 설명이 부실해 GitHub commerce-api-naver/commerce-api
Discussion #3526(네이버 담당자 답변, 2026-08 확인)로 보강했다:
  startSearchDate, endSearchDate : yyyy-MM-dd (둘 다 필수)
  page   : 1 이상
  size   : 10~200
  answered : true/false 생략시 전체

응답 필드는 문서에 명시가 없어(위 토론에서도 확인 못함) register.py/orders.py와
같은 방식으로 후보 키 여러 개를 시도해 방어적으로 파싱한다 — 실제 문의가 들어오면
반드시 눈으로 구조를 확인하고 필요하면 후보 키를 추가할 것.
"""
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE_URL = "https://api.commerce.naver.com/external"


@dataclass
class ProductInquiry:
    inquiry_id: str
    product_name: str
    content: str
    answered: bool
    questioner_name: str = ""
    created_date: str = ""
    answer_content: str = ""


def _get(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def fetch_inquiries(
    access_token: str,
    days: int = 7,
    answered: Optional[bool] = None,
    size: int = 100,
) -> List[ProductInquiry]:
    """최근 N일 상품문의 조회. answered=False로 미답변만 걸러볼 수 있다."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    end = date.today()
    start = end - timedelta(days=days)
    params = {
        "startSearchDate": start.isoformat(),
        "endSearchDate": end.isoformat(),
        "page": 1,
        "size": max(10, min(size, 200)),
    }
    if answered is not None:
        params["answered"] = "true" if answered else "false"

    resp = requests.get(
        f"{_BASE_URL}/v1/pay-user/inquiries",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"상품문의 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    items = data.get("contents") or data.get("data") or data.get("inquiries") or []
    if isinstance(items, dict):
        items = items.get("content", [])

    results = []
    for item in items:
        results.append(ProductInquiry(
            inquiry_id=str(_get(item, "inquiryNo", "id", "inquiryId")),
            product_name=_get(item, "productName", "originProductName"),
            content=_get(item, "inquiryContent", "content", "questionContent"),
            answered=bool(_get(item, "answered", "isAnswered", default=False)),
            questioner_name=_get(item, "maskingWriterId", "writerName", "questionerName"),
            created_date=_get(item, "createDate", "regDate", "inquiryRegistrationDateTime"),
            answer_content=_get(item, "answerContent", "answer"),
        ))
    return results


def _demo() -> None:
    """실행 가능한 자체 점검 — 응답 파싱만 검증 (네트워크 호출 없음)."""
    fake_item = {
        "inquiryNo": "12345", "productName": "실리콘주걱", "inquiryContent": "재질이 뭔가요?",
        "answered": False, "writerName": "홍길동", "createDate": "2026-08-15T10:00:00",
    }
    assert _get(fake_item, "inquiryNo", "id") == "12345"
    assert _get(fake_item, "id", "inquiryNo") == "12345", "후보 키 순서와 무관하게 존재하는 값을 찾아야 함"
    assert _get(fake_item, "missing", "alsoMissing", default="폴백") == "폴백"
    print("inquiries self-check OK")


if __name__ == "__main__":
    _demo()
