"""카테고리 매칭 신뢰 기준 자체 점검 — 실행: python3 -m bebrave.smartstore.test_category

16건 실측(2026-09)에서 나온 오분류 사례를 합성 카테고리 트리로 고정 재현한다.
1~2글자 카테고리명(무/톱/자)이 검색어에 부분일치로 걸리거나, "오일"/"영양제"/"리본"처럼
2글자 이상이어도 도매매 자체 분류와 아무 세그먼트도 안 겹치는 매칭은 전부 빈 값을
반환해야 한다(등록 차단). 정상 매칭(세그먼트 겹침·이름 완전일치)은 그대로 통과해야 한다.
"""
from . import category

_BAD_ONLY_TREE = [
    {"id": "BAD_MU", "name": "무", "wholeCategoryName": "식품>농산물>채소>무", "last": True},
    {"id": "BAD_TOP", "name": "톱", "wholeCategoryName": "생활/건강>공구>목공공구>톱", "last": True},
    {"id": "BAD_JA", "name": "자", "wholeCategoryName": "문구/사무용품>문구용품>자", "last": True},
    {"id": "BAD_OIL", "name": "오일", "wholeCategoryName": "스포츠/레저>자전거>자전거용품>오일", "last": True},
    {"id": "BAD_NUTRI", "name": "영양제", "wholeCategoryName": "반려동물>강아지 건강/관리용품>영양제", "last": True},
    {"id": "BAD_RIBBON", "name": "리본", "wholeCategoryName": "생활/건강>공구>포장용품>리본", "last": True},
]

_GOOD_TREE = _BAD_ONLY_TREE + [
    {"id": "GOOD_NAIL", "name": "네일케어용품", "wholeCategoryName": "뷰티>네일아트>네일케어용품", "last": True},
    {"id": "GOOD_CLIPPER", "name": "손톱깎이", "wholeCategoryName": "생활/건강>미용용품>손톱깎이", "last": True},
]


def _get(tree, keyword, domemae_category):
    real = category._load_category_tree
    category._load_category_tree = lambda access_token: tree
    try:
        return category.get_category_id(keyword, domemae_category, "x")
    finally:
        category._load_category_tree = real


def test():
    # 실측 오분류 16건 중 대표 사례 — 카테고리명 1글자(무/톱/자)는 길이 하한에서
    # 바로 걸러진다. 대안 카테고리가 트리에 아예 없는 상태에서도 빈 값을 반환해야 한다
    # (오분류로 올리는 것보다 안 올리는 게 낫다는 기존 게이트 원칙).
    assert _get(_BAD_ONLY_TREE, "큐티클리무버", "뷰티>네일아트>큐티클관리") == ""
    assert _get(_BAD_ONLY_TREE, "며느리발톱", "뷰티>네일아트>발관리") == ""
    assert _get(_BAD_ONLY_TREE, "자석젤네일", "뷰티>네일아트>젤네일") == ""

    # 2글자 이상(오일/영양제/리본)이라도 도매매 자체 분류와 세그먼트가 하나도
    # 안 겹치면 부분일치 점수만으로는 통과 못 한다.
    assert _get(_BAD_ONLY_TREE, "큐티클오일", "뷰티>네일아트>큐티클관리") == ""
    assert _get(_BAD_ONLY_TREE, "손톱영양제", "뷰티>네일아트>손톱관리") == ""
    assert _get(_BAD_ONLY_TREE, "네일스티커", "뷰티>네일아트>스티커") == ""

    # 정상 매칭은 그대로 통과 — 도매매 분류와 세그먼트가 겹치면(뷰티/네일아트) 오분류
    # 카테고리(오일/영양제 등)가 트리에 같이 있어도 정확한 쪽이 이긴다.
    assert _get(_GOOD_TREE, "큐티클오일", "뷰티>네일아트>큐티클관리") == "GOOD_NAIL"

    # 도매매 카테고리가 비어 있어도 검색어와 카테고리명이 완전히 같으면(강한 근거) 통과.
    assert _get(_GOOD_TREE, "손톱깎이", "") == "GOOD_CLIPPER"

    print("ok")


if __name__ == "__main__":
    test()
