"""
등록 상품 ↔ 도매매 동기화 판정 자체 점검.
실행: python3 -m bebrave.smartstore.test_sync

네트워크를 타지 않도록 도매매 조회를 가짜로 갈아끼운다. 검증 대상은 판정 규칙이다:
품절/조회실패는 판매중지, 재고 감소는 조정, 도매가 상승으로 마진이 무너지면 경고.
"""
from types import SimpleNamespace

from . import sync

_BASE_RECORD = {
    "name": "테스트상품",
    "naver_product_id": "999",
    "domemae_goods_no": "12345",
    "sale_price": 6600,
    "supply_price": 890,
    "stock_quantity": 999,
}


def _fake_detail(stock=100, supply_price=890):
    return lambda goods_no: SimpleNamespace(stock=stock, supply_price=supply_price)


def _with_detail(fn, **kw):
    real = sync.fetch_product_detail
    sync.fetch_product_detail = _fake_detail(**kw)
    try:
        return fn()
    finally:
        sync.fetch_product_detail = real


def demo() -> None:
    rec = dict(_BASE_RECORD)

    # 품절 → 판매중지
    r = _with_detail(lambda: sync.check_product(rec), stock=0)
    assert r.action == sync.ACTION_SUSPEND, r

    # 도매매 조회 실패 → "확인실패"(판매중지 아님) — 조회 안 된 것과 상품이 실제로
    # 내려간 것은 다르다. API 키 미설정 등으로 조회 실패를 판매중지로 판정하면 연동이
    # 끊긴 순간 일괄 반영 한 번에 멀쩡한 상품이 전부 내려간다(sync.py 주석 참고,
    # 이 파일은 옛 의도된 동작을 그대로 두고 있던 낡은 테스트였다 — 2026-09 정정).
    real = sync.fetch_product_detail
    sync.fetch_product_detail = lambda n: (_ for _ in ()).throw(ValueError("없음"))
    try:
        r = sync.check_product(rec)
        assert r.action == sync.ACTION_ERROR, r
    finally:
        sync.fetch_product_detail = real

    # 재고가 등록값보다 적으면 조정 — 반영에 쓸 수치를 문자열이 아니라 값으로 들고 있어야 함
    r = _with_detail(lambda: sync.check_product(rec), stock=12)
    assert r.action == sync.ACTION_STOCK, r
    assert r.new_stock == 12, r

    # 재고가 충분하면 이상 없음
    r = _with_detail(lambda: sync.check_product(rec), stock=5000)
    assert r.action == sync.ACTION_OK, r

    # 도매가가 크게 올라 최소 마진을 깨면 경고 (자동으로 판매가를 올리지는 않음).
    # 재고는 등록값(999) 이상으로 둬서 재고조정과 안 겹치게 격리한다 — 재고조정이
    # 더 급한 조치라 함께 걸리면 그쪽이 우선 반환된다(check_product 참고).
    r = _with_detail(lambda: sync.check_product(rec), stock=5000, supply_price=6000)
    assert r.action == sync.ACTION_MARGIN_WARN, r
    assert "6,000" in r.detail

    # 도매가가 올랐어도 마진이 버티면 이상 없음으로 두되 변동은 알려준다
    r = _with_detail(lambda: sync.check_product(rec), stock=5000, supply_price=1200)
    assert r.action == sync.ACTION_OK, r
    assert "→" in r.detail

    # 도매가가 움직이고 마진도 살아있는데 재고까지 부족하면 — 재고조정이 우선돼야
    # 한다. 예전 버그는 여기서 마진 판정으로 바로 반환해 재고 비교를 건너뛰었다
    # (2026-09 발견·수정).
    r = _with_detail(lambda: sync.check_product(rec), stock=12, supply_price=1200)
    assert r.action == sync.ACTION_STOCK and r.new_stock == 12, \
        f"도매가 변동 중에도 재고조정이 우선돼야 함: {r}"

    # 도매매 상품번호가 없으면 대조 불가
    r = sync.check_product({**rec, "domemae_goods_no": ""})
    assert r.action == sync.ACTION_ERROR, r

    # dry_run이면 네이버에 반영하지 않는다 — apply_result가 불리면 안 됨
    called = []
    real_apply = sync.apply_result
    sync.apply_result = lambda result, token: called.append(result)
    try:
        _with_detail(lambda: sync.sync_all(dry_run=True, path=_TmpPath()), stock=0)
        assert called == [], "dry_run인데 반영이 실행됨"
    finally:
        sync.apply_result = real_apply

    print("test_sync: 통과")


class _TmpPath:
    """등록 기록 1건짜리 가짜 파일 경로."""
    def exists(self):
        return True

    def read_text(self, encoding="utf-8"):
        import json
        return json.dumps([_BASE_RECORD], ensure_ascii=False)


if __name__ == "__main__":
    demo()
