"""
Layer 2 — 네이버 데이터랩 쇼핑인사이트 API 연동.
월별 검색 트렌드를 조회해 상승/안정/하락 방향을 판단한다.

API: NAVER API HUB 검색어트렌드 (2026-08 developers.naver.com 개발자센터 종료로 이관)
     POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search
     종량제 (ncloud 콘솔에서 사용량/요금 확인)

사전 준비:
  1. https://www.ncloud.com 콘솔 > Application Services > NAVER API HUB 에서 Application 등록
  2. "NAVER 검색" 카테고리에서 필요한 API 활성화, "Data Lab > 검색어 트렌드" 활성화
  3. .env 파일에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 설정 (API 관리 > 인증 정보에서 확인)
"""
import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from ..config import MAX_SEASONAL_VARIANCE


@dataclass
class TrendResult:
    keyword: str
    direction: str          # 'up' | 'stable' | 'down'
    recent_avg: float       # 최근 3개월 평균 (상대 지수)
    prev_avg: float         # 이전 3개월 평균
    variance: float         # 월별 편차 (0.30 초과 시 계절성 의심)
    is_seasonal: bool       # 계절성 상품 판단 결과
    raw_data: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        arrow = {"up": "상승", "stable": "안정", "down": "하락"}.get(self.direction, "?")
        seasonal_str = " [계절성 주의]" if self.is_seasonal else ""
        return (
            f"트렌드: {arrow}{seasonal_str}\n"
            f"  최근 3개월 평균: {self.recent_avg:.1f} | 이전 3개월 평균: {self.prev_avg:.1f} "
            f"| 월별 편차: {self.variance:.0%}"
        )


def fetch_trend(keyword: str, client_id: str = "", client_secret: str = "") -> TrendResult:
    """
    최근 12개월 월별 검색 트렌드 조회.
    client_id/secret 미전달 시 환경변수 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 사용.
    requests 미설치 시 NotImplementedError 발생.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError(
            "requests 패키지가 필요합니다. `pip install requests` 후 재시도하세요."
        )

    cid = client_id or os.environ.get("NAVER_CLIENT_ID", "")
    csecret = client_secret or os.environ.get("NAVER_CLIENT_SECRET", "")
    if not cid or not csecret:
        raise ValueError(
            "네이버 API 키가 없습니다. .env 파일에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET을 설정하세요."
        )

    # 조회 기간: 12개월 전 ~ 어제
    end = date.today() - timedelta(days=1)
    start = date(end.year - 1, end.month, 1)

    # datalab/search (통합 검색어트렌드) 사용
    # → 쇼핑인사이트는 카테고리 코드 필요, 검색어트렌드는 키워드 직접 사용 가능
    payload = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "month",
        "keywordGroups": [
            {"groupName": keyword, "keywords": [keyword]}
        ],
    }
    headers = {
        "X-NCP-APIGW-API-KEY-ID": cid,
        "X-NCP-APIGW-API-KEY": csecret,
        "Content-Type": "application/json",
    }
    resp = requests.post(
        "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    if not results:
        raise ValueError(f"'{keyword}' 트렌드 데이터를 가져오지 못했습니다.")

    monthly = results[0].get("data", [])
    ratios = [m["ratio"] for m in monthly]

    return _analyze_trend(keyword, ratios, monthly)


def _analyze_trend(keyword: str, ratios: List[float], raw: List[dict]) -> TrendResult:
    if len(ratios) < 6:
        return TrendResult(keyword=keyword, direction="stable",
                           recent_avg=0, prev_avg=0, variance=0,
                           is_seasonal=False, raw_data=raw)

    recent = ratios[-3:]
    prev = ratios[-6:-3]
    recent_avg = sum(recent) / len(recent)
    prev_avg = sum(prev) / len(prev)

    # 계절성 = 변동계수(CV)가 크면서 + 특정 월에 뚜렷한 피크가 있을 때만 판정.
    # (max-min)/avg 방식은 정상 변동도 과민 감지하므로 CV + 피크 조건으로 교체.
    mean = statistics.mean(ratios)
    cv = statistics.pstdev(ratios) / mean if mean > 0 else 0
    median = statistics.median(ratios)
    peak = max(ratios)
    is_seasonal = cv > MAX_SEASONAL_VARIANCE and peak > 1.8 * median

    change_rate = (recent_avg - prev_avg) / prev_avg if prev_avg > 0 else 0
    if change_rate >= 0.15:
        direction = "up"
    elif change_rate <= -0.15:
        direction = "down"
    else:
        direction = "stable"

    return TrendResult(
        keyword=keyword,
        direction=direction,
        recent_avg=round(recent_avg, 2),
        prev_avg=round(prev_avg, 2),
        variance=round(cv, 4),
        is_seasonal=is_seasonal,
        raw_data=raw,
    )
