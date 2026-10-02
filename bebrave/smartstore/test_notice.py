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
    # 2026-09-30 샘플 10건에서 오분류됐던 실제 도매매 분류들 — 대분류 이름에 걸리면 안 된다
    assert rt("취미/도서>정원/원예용품>화분") == "ETC"                      # 예전: 도서
    assert rt("가구/인테리어>침구단품>베개>메모리폼베개") == "SLEEPING_GEAR"  # 예전: 가구
    assert rt("생활용품>욕실용품>욕실발판/욕실매트>욕실발판/매트") == "ETC"  # 예전: 주방(상품명 때문)
    assert rt("화장품>뷰티소품>헤어소품>헤어브러시") == "ETC"               # 예전: 패션잡화
    assert rt("스포츠/레저>요가/필라테스>기타요가용품") == "SPORTS_EQUIPMENT"
    assert rt("주방용품") == "KITCHEN_UTENSILS"   # 한 단계뿐이면 그 이름으로 판단

    from .register import clean_manufacturer, clean_model, importer_from, looks_like_supplier_code
    # 제조사 — 샘플 실제 값
    assert clean_manufacturer("ablecompany협력사") == ""
    assert clean_manufacturer("업체협력사") == ""
    assert clean_manufacturer("올뎃홈 협력업체") == ""
    assert clean_manufacturer("중국(OEM)") == ""
    assert clean_manufacturer("셀링온(OEM)") == "셀링온"
    assert clean_manufacturer("칠성산업") == "칠성산업"
    assert clean_manufacturer("수입판매원 (주)미래종합아울렛물류") == "(주)미래종합아울렛물류"
    assert clean_manufacturer("해당없음") == ""
    # 수입자 — 스스로 수입자라고 밝힌 값만
    assert importer_from("수입판매원 (주)미래종합아울렛물류") == "(주)미래종합아울렛물류"
    assert importer_from("셀링온(OEM)") == ""
    # 모델명 — 상품명 복사본·한글 일반명사는 버리고, 영문·숫자 모델명은 남긴다
    assert clean_model("큐티클 니퍼 니퍼 큐티클관리니퍼 풋케어 네일", "큐티클 니퍼 니퍼 큐티클관리니퍼 풋케어 네일") == ""
    assert clean_model("요가링(하드타입)", "종아리 요가링 마사지링 필라테스 스트레칭 하드타입 2P") == ""
    assert clean_model("샴푸 브러쉬", "샴푸 브러쉬 헤어 두피 마사지 브러시") == ""
    assert clean_model("해당없음") == ""
    assert clean_model("POIPOI프리미엄규조토", "규조토발매트") == "POIPOI프리미엄규조토"
    assert clean_model("HJ5169-366", "송풍구 브러시") == "HJ5169-366"
    assert clean_model("2P", "요가링 2P") == ""                      # 상품명 조각
    assert looks_like_supplier_code("dwa1168") and looks_like_supplier_code("HJ5169-366")
    assert not looks_like_supplier_code("POIPOI프리미엄규조토")
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
        # 모델명이 없으면 도매매 상품번호로 폴백하지 않는다(2026-09). 상세페이지에도 없으니
        # "참조"가 아니라 "해당없음"(2026-10). 인증 칸은 "참조"로 대신할 수 없다(2023 고시 개정).
        assert eb["etc"]["modelName"] == "해당없음"
        assert eb["etc"]["certificateDetails"] == "해당없음"

        # 알 수 없는 유형이 들어와도 ETC로 안전하게 떨어져야 함
        u = notice.build_provided_notice(_product(), "fake-token", notice_type="NOT_A_TYPE")
        assert u["productInfoProvidedNoticeType"] == "ETC"

        # 제조사 칸의 "협력사" 값은 고시에 나가지 않고 폴백
        # — 대신 확인된 사실(제조국)만 적는다(2026-10). 제조국도 모르면 폴백.
        j = notice.build_provided_notice(_product(manufacturer="ablecompany협력사", domemae_category="패션잡화>양말",
                                                  origin_country="수입산_아시아_중국"), "fake-token")
        assert j["wear"]["manufacturer"] == "중국 제조"
        j2 = notice.build_provided_notice(_product(manufacturer="ablecompany협력사", domemae_category="패션잡화>양말"), "fake-token")
        assert j2["wear"]["manufacturer"] == "상세페이지 참조"
        # 국산은 행정구역 조각("종로구")이 아니라 "대한민국"
        assert notice._field_value("producer", _product(origin_country="국산_서울특별시_종로구")) == "대한민국"
        # 제조사 칸의 "상세페이지 참조"는 제조사 이름이 아니다
        from .register import clean_manufacturer, clean_model
        assert clean_manufacturer("상세페이지 참조") == "" and clean_manufacturer("중국OEM") == ""
        # 공급사 관리코드·숫자뿐인 값은 모델명에서 뺀다
        assert clean_model("DSJJWB7001360") == "" and clean_model("megaWB5072450") == ""
        assert clean_model("0671") == "" and clean_model("(BD-2331)") == "BD-2331"

        # 원산지가 나라가 아니면 제조국으로 쓰지 않는다
        assert notice._field_value("producer", _product(origin_country="상세정보별도표기")) is None
    finally:
        notice._load_notice_specs = real

    print("test_notice: 통과")


if __name__ == "__main__":
    demo()
