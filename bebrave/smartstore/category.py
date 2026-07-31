"""
스마트스토어 leafCategoryId 결정 — 실제 네이버 커머스 API 카테고리 트리 기반 검색.

이전 버전은 CATEGORY_MAP에 손으로 지어낸 ID를 하드코딩해뒀었는데, 전부 실제와 달랐음
(예: "주방용품"이라고 매핑해둔 50000803은 실제로는 "패션의류>여성의류>티셔츠" — 2026-07-12
실전 등록 테스트에서 발견). 이제는 전체 카테고리 트리(5,800여개)를 API로 가져와 로컬에
캐시해두고, 키워드로 실제 매칭되는 leaf 카테고리를 찾는다.

API: GET https://api.commerce.naver.com/external/v1/categories (전체 트리, searchKeyword
파라미터는 무시되는 것으로 확인됨 — 항상 전체 목록 반환)
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_CATEGORIES_URL = "https://api.commerce.naver.com/external/v1/categories"
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "naver_categories_cache.json"
_CACHE_TTL_SECONDS = 14 * 24 * 3600  # 2주


def _load_category_tree(access_token: str) -> List[dict]:
    """캐시가 있고 신선하면 재사용, 아니면 API로 전체 트리를 가져와 캐시."""
    if _CACHE_PATH.exists():
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age < _CACHE_TTL_SECONDS:
            try:
                return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.get(_CATEGORIES_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    resp.raise_for_status()
    tree = resp.json()

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    return tree


def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[\s/>,]+", text) if len(t) >= 2]


def get_category_id(
    keyword: str,
    domemae_category: str,
    access_token: str,
) -> str:
    """
    키워드 + 도매매 카테고리명으로 실제 스마트스토어 leaf 카테고리를 검색해 ID 반환.
    확실한 매치가 없으면 빈 문자열을 반환하니, 호출부에서 반드시 확인 후 사용할 것
    (예전처럼 아무 카테고리나 기본값으로 밀어넣지 않음 — 잘못된 카테고리 등록 방지).
    """
    tree = _load_category_tree(access_token)
    leaves = [c for c in tree if c.get("last")]

    query_terms = _tokenize(f"{keyword} {domemae_category}")
    if not query_terms:
        return ""

    # 도매매 자체 분류 경로("패션잡화>패션소품>우산>자동우산")가 있으면 최우선 신호로 사용.
    # 네이버 카테고리 경로와 세그먼트 단위로 겹치는 정도를 점수화 — 단어 하나만 우연히
    # 일치하는 얕은 매칭(예: "우산"만 겹치는 유아동잡화>우산)보다 경로 전체가 일치하는
    # 깊은 매칭(패션잡화>패션소품>우산>자동우산)이 압도적으로 이기도록 함
    # (2026-07-12: 골프우산이 "출산/육아>유아동잡화>우산"으로 잘못 매칭된 걸 보고 수정).
    domeme_segments = set(_tokenize(domemae_category.replace(">", " ")))

    best_id = ""
    best_score = 0
    for c in leaves:
        name = c.get("name", "")
        whole = c.get("wholeCategoryName", "")
        whole_segments = set(_tokenize(whole.replace(">", " ")))

        score = len(domeme_segments & whole_segments) * 50

        for t in query_terms:
            if t == name:
                score += 100
            elif t in name or name in t:
                score += 30
            elif t in whole:
                score += 5
        if score > best_score:
            best_score = score
            best_id = c.get("id", "")

    # 최소 신뢰 기준: name 자체와 부분일치라도 있어야 함 (score>=30). 그보다 낮으면
    # wholeCategoryName에서만 우연히 겹친 낮은 신뢰도 매칭이라 빈 값 반환.
    if best_score >= 30:
        return best_id
    return ""


def describe_category(category_id: str, access_token: str) -> str:
    """카테고리 ID → 전체 경로명 (등록 전 사람이 눈으로 확인하는 용도)."""
    tree = _load_category_tree(access_token)
    for c in tree:
        if c.get("id") == category_id:
            return c.get("wholeCategoryName", "")
    return ""

