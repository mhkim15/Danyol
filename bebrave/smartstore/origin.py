"""
원산지 코드 결정 — 네이버 커머스 API 원산지 코드표 기반.

이전 버전은 originAreaCode를 "03"으로 하드코딩하고 주석에 "03(국산)"이라고 적어뒀는데,
실제 코드표를 조회해보니 **03은 "상세설명에 표시"**였다(2026-08-10 확인). 국산은 00,
수입산은 02 하위 코드다. 즉 원산지가 국내산인지 검사하는 로직이 붙어 있었지만 정작
넣는 값은 원산지와 무관한 "상세설명에 표시"였고, 상세설명에 원산지를 실제로 적었는지는
아무도 보장하지 않았다.

도매매는 detail.country에 "수입산_아시아_중국" 형태로 원산지를 정확히 준다. 네이버
코드표의 "수입산:아시아>중국"(0200037)과 구분자만 다르므로, 변환해서 정확한 코드를 찾는다.

API: GET https://api.commerce.naver.com/external/v1/product-origin-areas (535건)
코드 체계: 00 국산 / 01 원양산 / 02 수입산 / 03 상세설명에 표시 / 04 직접입력 /
           05 원산지 표기 의무대상 아님. 하위는 "00 국산 → 0001 국산:강원특별자치도 →
           0001110 국산:강원특별자치도>춘천시" 처럼 이름에 : 와 > 로 계층을 표현.
"""
import json
import time
from pathlib import Path
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_ORIGIN_URL = "https://api.commerce.naver.com/external/v1/product-origin-areas"
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "naver_origin_areas_cache.json"
_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30일 — 국가/행정구역 목록이라 카테고리보다 덜 바뀜


def _load_origin_areas(access_token: str) -> List[dict]:
    """캐시가 있고 신선하면 재사용, 아니면 API로 코드표를 가져와 캐시."""
    if _CACHE_PATH.exists():
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age < _CACHE_TTL_SECONDS:
            try:
                return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(_ORIGIN_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    resp.raise_for_status()
    areas = resp.json().get("originAreaCodeNames", [])

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(areas, ensure_ascii=False), encoding="utf-8")
    return areas


def _to_naver_name(domemae_country: str) -> str:
    """
    도매매 원산지 표기 → 네이버 코드표 이름.
      "수입산_아시아_중국" → "수입산:아시아>중국"
      "국산_경기도_이천시" → "국산:경기도>이천시"
      "국산"               → "국산"
    """
    parts = [p for p in str(domemae_country).strip().split("_") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts[0] + ":" + ">".join(parts[1:])


def resolve_origin_code(domemae_country: str, access_token: str) -> str:
    """
    도매매 원산지 문자열 → 네이버 원산지 코드. 못 찾으면 빈 문자열.

    정확히 일치하는 게 없으면 뒤에서부터 한 단계씩 줄여가며 상위 지역으로 매칭한다
    (예: "수입산:아시아>중국>광둥성"이 코드표에 없으면 "수입산:아시아>중국"으로).
    끝까지 못 찾으면 빈 문자열 — 호출부에서 등록을 막을 것.
    """
    name = _to_naver_name(domemae_country)
    if not name:
        return ""

    areas = _load_origin_areas(access_token)
    by_name = {a.get("name", ""): a.get("code", "") for a in areas}

    if name in by_name:
        return by_name[name]

    # 하위 지역이 코드표에 없으면 상위로 한 단계씩 올라가며 재시도
    while ">" in name:
        name = name.rsplit(">", 1)[0]
        if name in by_name:
            return by_name[name]
    if ":" in name:
        name = name.split(":", 1)[0]
        if name in by_name:
            return by_name[name]
    return ""


def build_origin_area_info(
    domemae_country: str,
    access_token: str,
    importer: str = "",
) -> dict:
    """
    등록 요청의 originAreaInfo 생성. 원산지를 코드표에서 못 찾으면 ValueError를 던져
    등록을 막는다 — 원산지 표시법 위반을 피하기 위해 추측값으로 밀어넣지 않는다.

    importer(수입사)는 수입산일 때 네이버가 요구할 수 있어 받아두지만, 도매매가 주는
    값이 없으면 빈 문자열로 나간다. 실전 등록에서 거부되면 그때 필수임이 확인되는 것.
    """
    code = resolve_origin_code(domemae_country, access_token)
    if not code:
        raise ValueError(
            f"원산지 '{domemae_country}'를 네이버 코드표에서 찾지 못함 — "
            "잘못된 원산지 표시를 막기 위해 자동 등록 금지"
        )

    info = {"originAreaCode": code, "content": ""}
    # 수입산(02 계열)일 때만 수입사 정보를 채운다 — 국산에 importer를 넣으면 의미가 안 맞음
    if code.startswith("02") and importer:
        info["importer"] = importer
    return info
