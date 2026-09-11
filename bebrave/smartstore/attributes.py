"""
카테고리별 상품 속성(productAttributes) 조회 + 매칭.

네이버쇼핑 SEO 가이드(2026-08)가 "필터 결과 최상단 노출"의 조건으로 명시하는 항목인데,
지금까지 등록 요청에서 이 필드 자체를 아예 안 보내고 있었다(2026-09 코드 감사에서 발견).
category.py/origin.py/notice.py와 같은 캐시 패턴(전체 조회 → 로컬 캐시 → 재사용)을 쓴다.

ponytail: 커머스 API가 IP 허용목록 미등록(403)이라 조회 API의 실제 경로·응답 구조를
실측하지 못했다. 아래 _ATTR_URL_CANDIDATES는 네이버 공식 문서/커뮤니티에서 확인한
패턴 추정이다 — IP 허용목록 등록 후 실제 상품 1건으로 실측해 확정할 것. 그 전까지는
전부 실패해도 조용히 빈 리스트를 반환하므로 등록 자체는 막히지 않는다(원산지·A/S
연락처처럼 값이 없으면 허위 표시가 되는 필드가 아니라, 없어도 등록은 되는 필드라서
게이트로 만들지 않았다).
"""
import json
import re
import time
from pathlib import Path
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ponytail: 실측 전 추정 경로 — 성공한 경로를 캐시에 함께 적어두고 다음부터 그것만 시도한다.
_ATTR_URL_CANDIDATES = [
    "https://api.commerce.naver.com/external/v1/categories/{category_id}/attributes",
    "https://api.commerce.naver.com/external/v1/product-attributes?categoryId={category_id}",
]
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "naver_attributes_cache"
_CACHE_TTL_SECONDS = 14 * 24 * 3600  # 2주 — category.py와 동일 주기

MAX_ATTRIBUTES_PER_PRODUCT = 10  # 가이드: 과도하게 많으면 적합도 감점


def _cache_path(category_id: str) -> Path:
    return _CACHE_DIR / f"{category_id}.json"


def fetch_category_attributes(category_id: str, access_token: str) -> List[dict]:
    """카테고리별 속성 스펙 조회. 실패하면 조용히 빈 리스트 — 등록을 막지 않는다.

    스펙 형태 추정(실측 전): [{"attributeSeq": int, "attributeName": str,
    "attributeValues": [{"attributeValueSeq": int, "attributeValue": str}, ...]}, ...]
    """
    if not category_id:
        return []

    path = _cache_path(category_id)
    if path.exists() and time.time() - path.stat().st_mtime < _CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not _HAS_REQUESTS:
        return []

    for url_template in _ATTR_URL_CANDIDATES:
        url = url_template.format(category_id=category_id)
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
            if resp.status_code != 200:
                continue
            specs = resp.json()
            if not isinstance(specs, list):
                specs = specs.get("attributes") or specs.get("data") or []
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(specs, ensure_ascii=False), encoding="utf-8")
            return specs
        except Exception:
            continue

    return []


def _haystack(name: str, option_group_name: str, options: List[dict]) -> str:
    option_names = " ".join(o.get("name", "") for o in (options or []))
    return f"{name} {option_group_name} {option_names}"


def match_attributes(specs: List[dict], name: str, option_group_name: str = "", options: Optional[List[dict]] = None) -> List[dict]:
    """속성값 이름이 상품명·옵션명에 그대로 등장할 때만 채택한다 — 추론·추정 금지
    (고시 항목에서 날짜·불리언을 지어내지 않는 것과 같은 원칙, notice.py 참고).
    숫자+단위 속성(attributeRealValue류)은 다루지 않는다 — 오기입이 신뢰도 제재 대상이다.

    Returns: [{"attributeSeq": int, "attributeValueSeq": int}, ...] (register.py가 그대로 전송)
    """
    haystack = _haystack(name, option_group_name, options or [])
    matched: List[dict] = []
    for spec in specs:
        attr_seq = spec.get("attributeSeq")
        if attr_seq is None:
            continue
        for value in spec.get("attributeValues") or []:
            value_name = value.get("attributeValue", "")
            value_seq = value.get("attributeValueSeq")
            if not value_name or value_seq is None:
                continue
            if value_name in haystack:
                matched.append({"attributeSeq": attr_seq, "attributeValueSeq": value_seq})
                break  # 속성 하나당 값 하나만 — 같은 속성에 여러 값을 중복 채택하지 않음
        if len(matched) >= MAX_ATTRIBUTES_PER_PRODUCT:
            break
    return matched


def _demo() -> None:
    """매칭 로직만 검증 — 네트워크 호출 없음."""
    specs = [
        {"attributeSeq": 1, "attributeName": "색상", "attributeValues": [
            {"attributeValueSeq": 101, "attributeValue": "화이트"},
            {"attributeValueSeq": 102, "attributeValue": "블랙"},
        ]},
        {"attributeSeq": 2, "attributeName": "소재", "attributeValues": [
            {"attributeValueSeq": 201, "attributeValue": "극세사"},
        ]},
        {"attributeSeq": 3, "attributeName": "계절", "attributeValues": [
            {"attributeValueSeq": 301, "attributeValue": "겨울"},
        ]},
    ]
    matched = match_attributes(specs, "JAJU 극세사 이불커버 화이트 겨울용")
    assert {"attributeSeq": 1, "attributeValueSeq": 101} in matched, matched
    assert {"attributeSeq": 2, "attributeValueSeq": 201} in matched, matched
    assert {"attributeSeq": 3, "attributeValueSeq": 301} in matched, matched
    assert len(matched) == 3, matched

    # 상품명에 없는 값은 채택하지 않는다 (추정 금지)
    matched2 = match_attributes(specs, "이불커버 단순 상품명")
    assert matched2 == [], matched2

    # 옵션명에서도 매칭된다
    matched3 = match_attributes(specs, "이불커버", options=[{"name": "블랙"}])
    assert {"attributeSeq": 1, "attributeValueSeq": 102} in matched3, matched3

    # API 응답이 없으면(카테고리 미확정 등) 빈 리스트 — 등록을 막지 않음
    assert fetch_category_attributes("", "token") == []


if __name__ == "__main__":
    _demo()
    print("OK - attributes self-check passed")
