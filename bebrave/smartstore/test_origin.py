"""
원산지 코드 매핑 자체 점검. 실행: python3 -m bebrave.smartstore.test_origin

네트워크 없이 돌도록 코드표를 가짜로 주입한다 — 검증 대상은 도매매 표기를 네이버
이름 형식으로 바꾸는 변환과, 하위 지역이 코드표에 없을 때 상위로 올라가는 폴백이다.
"""
from . import origin

_FAKE_AREAS = [
    {"code": "00", "name": "국산"},
    {"code": "0002500", "name": "국산:경기도>이천시"},
    {"code": "02", "name": "수입산"},
    {"code": "0200", "name": "수입산:아시아"},
    {"code": "0200037", "name": "수입산:아시아>중국"},
    {"code": "03", "name": "상세설명에 표시"},
]


def _patched(fn):
    """_load_origin_areas를 가짜 코드표로 갈아끼워 네트워크 없이 검증."""
    real = origin._load_origin_areas
    origin._load_origin_areas = lambda token: _FAKE_AREAS
    try:
        fn()
    finally:
        origin._load_origin_areas = real


def demo() -> None:
    assert origin._to_naver_name("수입산_아시아_중국") == "수입산:아시아>중국"
    assert origin._to_naver_name("국산") == "국산"
    assert origin._to_naver_name("") == ""

    r = lambda c: origin.resolve_origin_code(c, "fake-token")

    assert r("수입산_아시아_중국") == "0200037"
    assert r("국산") == "00"
    assert r("국산_경기도_이천시") == "0002500"

    # 코드표에 없는 하위 지역 → 상위로 폴백
    assert r("수입산_아시아_중국_광둥성") == "0200037"
    # 아시아까지만 아는 경우
    assert r("수입산_아시아_없는나라") == "0200"

    # 미표기·미상은 빈 값 → 호출부가 등록을 막아야 함
    assert r("") == ""
    assert r("알수없는나라") == ""

    # 03("상세설명에 표시")이 국산 자리에 잘못 들어가지 않는지 — 옛 버그 회귀 방지
    assert r("국산") != "03"

    info = origin.build_origin_area_info("수입산_아시아_중국", "fake-token", importer="(주)엘앤디")
    assert info["originAreaCode"] == "0200037"
    assert info["importer"] == "(주)엘앤디"

    # 국산에는 importer를 넣지 않는다
    assert "importer" not in origin.build_origin_area_info("국산", "fake-token", importer="(주)엘앤디")

    try:
        origin.build_origin_area_info("알수없는나라", "fake-token")
        raise AssertionError("원산지 미확정인데 등록 바디가 만들어짐 — 차단됐어야 함")
    except ValueError:
        pass

    print("test_origin: 통과")


if __name__ == "__main__":
    _patched(demo)
