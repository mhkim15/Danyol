"""중복 등록 방지 로직 자체 점검 — 실행: python3 -m bebrave.smartstore.test_pipeline

로컬 원장 파일만 보던 예전 방식은 파일이 없거나 깨지면 조용히 빈 값을 반환해
전 상품을 신규로 간주했다(2026-09 발견). 네이버 실제 등록 목록(product_status)을
우선 조회하고 로컬 파일과 합집합으로 합치는 새 로직을 검증한다 — 실제 네트워크
호출 없이 product_status.fetch_product_statuses를 monkeypatch로 대체한다.
"""
import json
import tempfile
from pathlib import Path

from . import pipeline
from . import product_status


def _with_fake_statuses(codes, fn):
    real = product_status.fetch_product_statuses
    product_status.fetch_product_statuses = lambda access_token: [
        product_status.ProductStatus(product_id=str(i), name="x", status_type="SALE", seller_management_code=c)
        for i, c in enumerate(codes)
    ]
    try:
        fn()
    finally:
        product_status.fetch_product_statuses = real


def test_merges_live_and_local():
    with tempfile.TemporaryDirectory() as d:
        local_path = Path(d) / "registered.json"
        local_path.write_text(json.dumps([{"domemae_goods_no": "111"}]), encoding="utf-8")

        def check():
            result = pipeline._load_registered_goods_nos(local_path, access_token="fake")
            assert result == {"111", "222"}, result
        _with_fake_statuses(["222"], check)


def test_falls_back_when_local_file_missing():
    # 로컬 파일이 아예 없어도(경로 실수·최초 실행 등) 네이버 실제 목록만으로
    # 중복 등록 방지가 유지돼야 한다 — 예전엔 조용히 빈 집합을 반환했다.
    missing_path = Path(tempfile.gettempdir()) / "이런파일없음_pipeline_test.json"
    assert not missing_path.exists()

    def check():
        result = pipeline._load_registered_goods_nos(missing_path, access_token="fake")
        assert result == {"333"}, result
    _with_fake_statuses(["333"], check)


def test_falls_back_when_live_api_fails():
    def _boom(access_token):
        raise RuntimeError("네트워크 실패(시뮬)")
    real = product_status.fetch_product_statuses
    product_status.fetch_product_statuses = _boom
    try:
        with tempfile.TemporaryDirectory() as d:
            local_path = Path(d) / "registered.json"
            local_path.write_text(json.dumps([{"domemae_goods_no": "444"}]), encoding="utf-8")
            result = pipeline._load_registered_goods_nos(local_path, access_token="fake")
            assert result == {"444"}, result
    finally:
        product_status.fetch_product_statuses = real


def test_cut_urls_never_reach_naver():
    """미리보기에서 만든 로컬 컷 주소가 상세페이지에 남아 나가면 구매자 화면의 사진이
    전부 깨진다 — 사람이 미리보기를 한 글자만 고쳐도 그 HTML이 통째로 등록에 실리므로
    반드시 치환을 거쳐야 한다(2026-09)."""
    from . import images as images_mod

    html = ('<div><img src="/cut/11013443/000.jpg"/>'
            '<img src="http://127.0.0.1:5050/cut/11013443/001.jpg"/>'
            '<img src="/cut/11013443/002.jpg"/></div>')

    real = images_mod.upload_images
    # 가운데 한 장은 업로드 실패 — 그 자리는 <img>째 빠져야 한다
    images_mod.upload_images = lambda paths, token, square_first=True: [
        "https://shop-phinf.pstatic.net/a.jpg", None, "https://shop-phinf.pstatic.net/c.jpg"]
    try:
        out, ok = pipeline._swap_cut_urls(html, token="fake")
    finally:
        images_mod.upload_images = real

    assert "/cut/" not in out, f"로컬 컷 주소가 상세페이지에 남음: {out}"
    assert "127.0.0.1" not in out, "로컬 서버 주소가 상세페이지에 남음"
    assert out.count("<img") == 2, f"업로드 못 한 컷의 빈 사진 자리가 남음: {out}"
    assert ok == ["https://shop-phinf.pstatic.net/a.jpg", "https://shop-phinf.pstatic.net/c.jpg"]

    # 컷 주소가 없는 상세페이지는 건드리지 않는다
    plain = '<div><img src="https://shop-phinf.pstatic.net/x.jpg"/></div>'
    same, none = pipeline._swap_cut_urls(plain, token="fake")
    assert same == plain and none == []


def test():
    test_merges_live_and_local()
    test_falls_back_when_local_file_missing()
    test_falls_back_when_live_api_fails()
    test_cut_urls_never_reach_naver()
    print("ok")


if __name__ == "__main__":
    test()
