"""
네이버 커머스 API 정산 조회 — 일별/건별 정산, 부가세 건별.

이 프로젝트가 지금까지 전혀 쓰지 않던 영역이다. 판매수수료·CS예비비는
config.py에 추정치로만 박혀 있었는데, 실제 입금액을 조회하면 그 추정을
실측으로 교정할 수 있다(bebrave/report/reconcile.py에서 대사).

공식 문서(apicenter.commerce.naver.com)는 fetch 차단으로 직접 확인 못 함 —
GitHub commerce-api-naver/commerce-api Discussion #3508, #3525, #414, #823,
#3258(2026-08 확인)로 아래를 교차 확인했다:

  일별 정산 GET /v1/pay-settle/settle/daily
    params: startDate, endDate (최대 1개월 범위) — 정산 "예정일" 기준으로 조회됨
    응답: elements[].benefitSettleAmount 등 (혜택정산 금액, 집계치)

  건별 정산 GET /v1/pay-settle/settle/case
    **조회 기간 최대 1일** — 한 달 대사하려면 날짜별로 반복 호출해야 함
    params: periodType (아래 3종 중 하나), startDate, endDate, productOrderId(선택)
      SETTLE_CASEBYCASE_SETTLE_BASIS_DATE     정산 기준일(구매확정/반품완료/빠른정산 수거일)
      SETTLE_CASEBYCASE_SETTLE_SCHEDULE_DATE  정산 예정일(은행 처리 예정) — 담당자 권장값
      SETTLE_CASEBYCASE_SETTLE_COMPLETE_DATE  정산 완료일(실입금 후에만 생성)
    응답: elements[].benefitSettleAmount는 건별에선 항상 0(집계는 daily에서만) —
      건별-일별 완전 대사는 네이버 쪽에서도 공식적으로 불가능하다고 확인됨.
    settleType: NORMAL_SETTLE_ORIGINAL(일반정산) / QUICK_SETTLE_ORIGINAL(빠른정산).
      발송완료 시점에 조건 충족하면 NORMAL→QUICK으로 사후 전환될 수 있음.

  부가세 건별 GET /v1/pay-settle/vat/case
    productOrderType으로 productOrderId의 의미가 갈림: PROD_ORDER(실제 상품주문번호) /
    DELIVERY(배송비 번호) / 그 외(기타비용 번호). 배송비→상품주문 매핑 API 없음.

⚠️ 위 파라미터명은 GitHub 답변 텍스트로 교차 확인한 것이지 실제 API 호출로
검증된 게 아니다 — 응답 파싱은 register.py/inquiries.py와 같은 방식으로
후보 필드명을 여러 개 시도하는 방어적 파싱을 쓴다. 정산은 사후에 취소 등으로
같은 productOrderId가 다른 settleType으로 "새 레코드"가 생기는 방식이라
(기존 레코드를 수정하지 않음), 변경분만 골라오는 API가 없다 — 최근 구간을
통째로 재조회해서 upsert하는 방식으로만 최신 상태를 유지할 수 있다.
"""
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE_URL = "https://api.commerce.naver.com/external"

PERIOD_TYPE_SCHEDULE = "SETTLE_CASEBYCASE_SETTLE_SCHEDULE_DATE"
PERIOD_TYPE_BASIS = "SETTLE_CASEBYCASE_SETTLE_BASIS_DATE"
PERIOD_TYPE_COMPLETE = "SETTLE_CASEBYCASE_SETTLE_COMPLETE_DATE"

SETTLE_TYPE_NORMAL = "NORMAL_SETTLE_ORIGINAL"
SETTLE_TYPE_QUICK = "QUICK_SETTLE_ORIGINAL"


def _get(d: dict, *keys, default=0):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class DailySettlement:
    settle_date: str                 # 정산예정일
    settle_amount: int = 0            # 정산 지급액 합계
    benefit_settle_amount: int = 0    # 혜택정산 금액(쿠폰/포인트 등, 집계만 — 세부내역 API 없음)
    raw: dict = field(default_factory=dict)


@dataclass
class CaseSettlement:
    product_order_id: str
    settle_date: str
    settle_amount: int = 0            # 실정산액(수수료 차감 후 실지급액)
    commission_amount: int = 0
    settle_type: str = ""             # NORMAL_SETTLE_ORIGINAL / QUICK_SETTLE_ORIGINAL
    raw: dict = field(default_factory=dict)


def fetch_daily_settlements(access_token: str, start: date, end: date) -> List[DailySettlement]:
    """정산예정일 기준 일별 합계. 최대 1개월 범위."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(
        f"{_BASE_URL}/v1/pay-settle/settle/daily",
        headers=_headers(access_token),
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"일별 정산 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    items = data.get("elements") or data.get("data") or data.get("contents") or []
    return [
        DailySettlement(
            settle_date=str(_get(it, "settleExpectDate", "settleDate", "date", default="")),
            settle_amount=int(_get(it, "settleAmount", "totalSettleAmount", "amount", default=0) or 0),
            benefit_settle_amount=int(_get(it, "benefitSettleAmount", default=0) or 0),
            raw=it,
        )
        for it in items
    ]


def fetch_case_settlements(
    access_token: str,
    target_date: date,
    period_type: str = PERIOD_TYPE_SCHEDULE,
) -> List[CaseSettlement]:
    """건별 정산 — 조회 기간 최대 1일이라 하루씩만 호출 가능. 월 단위 대사는 날짜별 반복 호출 필요."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(
        f"{_BASE_URL}/v1/pay-settle/settle/case",
        headers=_headers(access_token),
        params={
            "periodType": period_type,
            "startDate": target_date.isoformat(),
            "endDate": target_date.isoformat(),
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"건별 정산 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    items = data.get("elements") or data.get("data") or data.get("contents") or []
    return [
        CaseSettlement(
            product_order_id=str(_get(it, "productOrderId", "orderId", default="")),
            settle_date=str(_get(it, "settleExpectDate", "settleDate", default=target_date.isoformat())),
            settle_amount=int(_get(it, "settleExpectAmount", "settleAmount", "amount", default=0) or 0),
            commission_amount=int(_get(it, "commissionAmount", "totalCommissionAmount", default=0) or 0),
            settle_type=str(_get(it, "settleType", default="")),
            raw=it,
        )
        for it in items
    ]


def fetch_case_settlements_range(
    access_token: str,
    start: date,
    end: date,
    period_type: str = PERIOD_TYPE_SCHEDULE,
) -> List[CaseSettlement]:
    """건별 정산을 여러 날 조회 — /settle/case는 하루씩만 되므로 날짜별로 반복 호출한다.
    조회 범위가 넓으면 호출 수가 그만큼 늘어난다(예: 30일 = 30회)."""
    results: List[CaseSettlement] = []
    d = start
    while d <= end:
        results.extend(fetch_case_settlements(access_token, d, period_type))
        d += timedelta(days=1)
    return results


def fetch_vat_cases(access_token: str, start: date, end: date) -> List[dict]:
    """부가세 건별 내역. 응답 구조 미검증이라 원시 딕셔너리 리스트로 반환."""
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(
        f"{_BASE_URL}/v1/pay-settle/vat/case",
        headers=_headers(access_token),
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"부가세 조회 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    return data.get("elements") or data.get("data") or data.get("contents") or []


def vat_amount(case: dict) -> int:
    """부가세 건별 응답 1건에서 세액을 뽑는다 — 응답 구조 미검증이라 후보 필드명을 순회."""
    return int(_get(case, "vatAmount", "supplyVatAmount", "taxAmount", "amount", default=0) or 0)


def _demo() -> None:
    """실행 가능한 자체 점검 — 방어적 파싱만 검증 (네트워크 호출 없음)."""
    assert _get({"settleAmount": 1000}, "settleExpectAmount", "settleAmount") == 1000
    assert _get({}, "a", "b", default=0) == 0
    assert _get({"a": None, "b": 5}, "a", "b") == 5, "None 값은 건너뛰고 다음 후보를 찾아야 함"
    print("settlement self-check OK")


if __name__ == "__main__":
    _demo()
