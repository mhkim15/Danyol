"""
상품정보제공고시 유형 결정 + 항목 채우기 자체 점검.
실행: python3 -m bebrave.smartstore.test_notice
"""
from types import SimpleNamespace

from . import notice

_FAKE_SPECS = [
    {
        "productInfoProvidedNoticeType": "WEAR",
        "productInfoProvidedNoticeContents": [
            {"fieldName": "material", "fieldType": "String", "fieldMaxLength": 1500},
            {"fieldName": "manufacturer", "fieldType": "String", "fieldMaxLength": 200},
            {"fieldName": "packDate", "fieldType": "YearMonth"},
            {"fieldName": "packDateText", "fieldType": "String", "fieldMaxLength": 200},
            {"fieldName": "afterServiceDirector", "fieldType": "String", "fieldMaxLength": 200},
        ],
    },
    {
        "productInfoProvidedNoticeType": "KITCHEN_UTENSILS",
        "productInfoProvidedNoticeContents": [
            {"fieldName": "itemName", "fieldType": "String", "fieldMaxLength": 10},
            {"fieldName": "producer", "fieldType": "String", "fieldMaxLength": 200},
            {"fieldName": "importDeclaration", "fieldType": "Boolean"},
        ],
    },
    {
        "productInfoProvidedNoticeType": "ETC",
        "productInfoProvidedNoticeContents": [
            {"fieldName": "itemName", "fieldType": "String", "fieldMaxLength": 200},
            {"fieldName": "modelName", "fieldType": "String", "fieldMaxLength": 200},
            {"fieldName": "certificateDetails", "fieldType": "String", "fieldMaxLength": 200},
        ],
    },
]


def _product(**kw):
    base = dict(name="테스트상품", model="", manufacturer="", origin_country="",
                domemae_goods_no="12345", domemae_category="", keyword="")
    base.update(kw)
    return SimpleNamespace(**base)


def demo() -> None:
    rt = notice.resolve_notice_type
    assert rt("패션잡화>양말>여성양말>덧신") == "WEAR"
    assert rt("주방용품>조리도구>주걱") == "KITCHEN_UTENSILS"
    assert rt("패션잡화>패션소품>우산>자동우산") == "FASHION_ITEMS"
    assert rt("생활용품>정리수납>알수없음") == "ETC"          # 확신 없으면 ETC
    assert notice._node_name("KITCHEN_UTENSILS") == "kitchenUtensils"
    assert notice._node_name("ETC") == "etc"
    assert notice._node_name("WEAR") == "wear"

    real = notice._load_notice_specs
    notice._load_notice_specs = lambda token: _FAKE_SPECS
    try:
        p = _product(manufacturer="(주)엘앤디", origin_country="수입산_아시아_중국",
                     domemae_category="패션잡화>양말")
        body = notice.build_provided_notice(p, "fake-token")
        assert body["productInfoProvidedNoticeType"] == "WEAR"
        w = body["wear"]
        # 도매매에서 찾은 값은 그대로, 못 찾은 항목은 폴백으로 빠짐없이 채워야 함
        assert w["manufacturer"] == "(주)엘앤디"
        assert w["material"] == "상세페이지 참조"
        # 날짜/불리언 항목은 지어내지 않고 생략
        assert "packDate" not in w
        assert w["packDateText"] == "상세페이지 참조"

        # 제조국은 원산지 표기의 마지막 조각
        k = _product(domemae_category="주방용품>주걱", origin_country="수입산_아시아_중국")
        kb = notice.build_provided_notice(k, "fake-token")
        assert kb["productInfoProvidedNoticeType"] == "KITCHEN_UTENSILS"
        assert kb["kitchenUtensils"]["producer"] == "중국"
        assert "importDeclaration" not in kb["kitchenUtensils"]
        # fieldMaxLength 초과분은 잘라야 함 (itemName 최대 10자)
        assert len(kb["kitchenUtensils"]["itemName"]) <= 10

        # ETC도 필수 항목을 빠짐없이 채운다 — 예전엔 certificateDetails가 빠져 있었음
        e = _product(domemae_category="알수없는분류")
        eb = notice.build_provided_notice(e, "fake-token")
        assert eb["productInfoProvidedNoticeType"] == "ETC"
        assert set(eb["etc"]) == {"itemName", "modelName", "certificateDetails"}
        assert eb["etc"]["modelName"] == "12345"      # 모델명 없으면 도매매 상품번호

        # 알 수 없는 유형이 들어와도 ETC로 안전하게 떨어져야 함
        u = notice.build_provided_notice(_product(), "fake-token", notice_type="NOT_A_TYPE")
        assert u["productInfoProvidedNoticeType"] == "ETC"
    finally:
        notice._load_notice_specs = real

    print("test_notice: 통과")


if __name__ == "__main__":
    demo()
