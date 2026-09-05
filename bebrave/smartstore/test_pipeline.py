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


def test():
    test_merges_live_and_local()
    test_falls_back_when_local_file_missing()
    test_falls_back_when_live_api_fails()
    print("ok")


if __name__ == "__main__":
    test()
