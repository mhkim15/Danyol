"""
등록 상품 ↔ 도매매 동기화 — 품절 감지와 도매가 변동 감지.

등록할 때 재고를 999로 박아넣고 그 뒤로 아무도 안 보던 구조였다(2026-08-10 확인:
등록된 2건 모두 재고 999). 도매매에서 품절돼도 스마트스토어는 계속 판매중이라
주문을 받고 나서 발주가 안 되고, 그러면 발송 지연이나 판매자 귀책 취소로 이어져
굿서비스 점수가 깎인다. 위탁판매에서 계정이 망가지는 가장 흔한 경로다.

도매가도 마찬가지로 등록 시점 값으로 판매가를 정하고 끝이라, 공급사가 도매가를
올리면 마진이 무너지는 걸 알 방법이 없었다.

정책:
  - 품절(재고 0)이거나 도매매에서 상품이 사라지면 → 즉시 판매중지
  - 재고가 등록값보다 적으면 → 그 수량으로 낮춤
  - 도매가가 올라 최소 마진 미달이면 → 경고만. 판매가 인상은 노출 순위에 영향을
    주므로 사람이 판단할 일이라 자동으로 올리지 않는다.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..config import MIN_MARGIN, FREE_SHIPPING_THRESHOLD
from ..margin.calculator import calculate, estimate_sale_price
from ..sourcing.domemae import fetch_product_detail
from .register import update_registered_product

_REGISTERED_PATH = Path("data/registered_products.json")

# 판매중지로 내려야 하는 사유 (사람이 다시 켜야 하는 것들)
ACTION_SUSPEND = "판매중지"
ACTION_STOCK = "재고조정"
ACTION_MARGIN_WARN = "마진경고"
ACTION_OK = "이상없음"
ACTION_ERROR = "확인실패"


@dataclass
class SyncResult:
    naver_product_id: str
    name: str
    action: str
    detail: str
    new_stock: Optional[int] = None   # ACTION_STOCK일 때 반영할 재고 수량
    suggested_price: Optional[int] = None  # ACTION_MARGIN_WARN일 때 목표마진 회복 참고가(자동 반영 안 함)
    # 도매처 실재고 — 위탁판매는 내가 재고를 안 갖고 있어서 이 숫자가 곧 판매 가능 수량이다.
    # 예전엔 "이상없음"이면 저장조차 안 해, 도매처에 3개 남은 상품(=곧 품절)을
    # 0이 될 때까지 알 수 없었다.
    supply_stock: Optional[int] = None
    supply_price: Optional[int] = None

    def line(self) -> str:
        mark = {
            ACTION_SUSPEND: "[중지]",
            ACTION_STOCK: "[재고]",
            ACTION_MARGIN_WARN: "[마진]",
            ACTION_ERROR: "[오류]",
        }.get(self.action, "      ")
        return f"{mark} {self.name[:32]:34} {self.detail}"


def _load_registered(path: Optional[Path] = None) -> List[dict]:
    p = path or _REGISTERED_PATH
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def check_product(record: dict) -> SyncResult:
    """
    등록 기록 1건을 도매매와 대조해 필요한 조치를 판정. 네이버 쪽은 건드리지 않는다
    (판정과 반영을 나눠서, dry-run으로 판정만 볼 수 있게 함).
    """
    pid = str(record.get("naver_product_id", ""))
    name = record.get("name", "")
    goods_no = str(record.get("domemae_goods_no", ""))

    if not goods_no:
        return SyncResult(pid, name, ACTION_ERROR, "도매매 상품번호가 기록에 없음 — 대조 불가")

    try:
        p = fetch_product_detail(goods_no)
    except Exception as e:
        # 조회가 안 됐다는 것과 상품이 내려갔다는 것은 다르다. API 키 미설정·네트워크
        # 오류·레이트리밋도 모두 여기로 오는데, 이걸 "판매중지"로 판정하면 일괄 반영
        # 한 번에 멀쩡한 상품이 전부 내려가 매출이 통째로 멈춘다(실제로 API 키가
        # 빠진 상태에서 전 상품이 판매중지 판정으로 나왔다).
        # 판매중지는 되돌리기 번거롭고 노출 순위에도 영향이 있으므로, 원인을 사람이
        # 확인하도록 "확인실패"로 남기고 자동 반영 대상에서 뺀다.
        return SyncResult(pid, name, ACTION_ERROR,
                          f"도매매 조회 실패 ({type(e).__name__}: {str(e)[:60]}) — "
                          f"공급 중단인지 연동 문제인지 확인 필요")

    # 판정이 뭐로 끝나든 도매처 재고·도매가는 항상 담는다 — 화면이 "몇 개 남았나"를
    # 보여줄 수 있어야 품절 임박을 미리 잡는다.
    stock_info = {"supply_stock": p.stock, "supply_price": p.supply_price}

    if p.stock <= 0:
        return SyncResult(pid, name, ACTION_SUSPEND, "도매매 품절 — 판매중지", **stock_info)

    # 도매가 변동 → 현재 판매가 기준으로 마진 재계산
    sale_price = int(record.get("sale_price", 0) or 0)
    old_cost = int(record.get("supply_price", 0) or 0)
    if sale_price and p.supply_price and p.supply_price != old_cost:
        m = calculate(sale_price=sale_price, cost_price=p.supply_price,
                      free_shipping=(sale_price >= FREE_SHIPPING_THRESHOLD))
        moved = p.supply_price - old_cost
        if not m.passes_min:
            # 판매가를 자동으로 올리지는 않는다(노출 순위·구매전환에 영향) — 참고용 권장가만 계산해 보여준다.
            suggested = estimate_sale_price(p.supply_price)
            return SyncResult(
                pid, name, ACTION_MARGIN_WARN,
                f"도매가 {old_cost:,}→{p.supply_price:,}원({moved:+,}) "
                f"마진 {m.margin_rate:.1%} < 최소 {MIN_MARGIN:.0%} — 현재가 {sale_price:,}원, 목표마진 회복가 {suggested:,}원 참고",
                suggested_price=suggested, **stock_info,
            )
        return SyncResult(
            pid, name, ACTION_OK,
            f"도매가 {old_cost:,}→{p.supply_price:,}원({moved:+,}) 마진 {m.margin_rate:.1%} 유지",
            **stock_info,
        )

    registered_stock = int(record.get("stock_quantity", 0) or 0)
    if p.stock < registered_stock:
        return SyncResult(pid, name, ACTION_STOCK,
                          f"재고 {registered_stock:,}→{p.stock:,}개로 조정",
                          new_stock=p.stock, **stock_info)

    return SyncResult(pid, name, ACTION_OK, f"도매가 {p.supply_price:,}원 — 이상없음", **stock_info)


def apply_result(result: SyncResult, access_token: str) -> None:
    """판정 결과를 스마트스토어에 반영. 마진경고는 알림만이라 반영할 게 없다."""
    if result.action == ACTION_SUSPEND:
        def mutate(body):
            body["originProduct"]["statusType"] = "SUSPENSION"
            if "smartstoreChannelProduct" in body:
                body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] = "SUSPENSION"
        update_registered_product(result.naver_product_id, access_token, mutate)

    elif result.action == ACTION_STOCK and result.new_stock is not None:
        def mutate(body):
            body["originProduct"]["stockQuantity"] = result.new_stock
        update_registered_product(result.naver_product_id, access_token, mutate)


def sync_all(access_token: str = "", dry_run: bool = True,
             path: Optional[Path] = None) -> List[SyncResult]:
    """등록 상품 전체를 도매매와 대조. dry_run이면 판정만 하고 반영하지 않는다."""
    results = []
    for record in _load_registered(path):
        r = check_product(record)
        if not dry_run and r.action in (ACTION_SUSPEND, ACTION_STOCK):
            try:
                apply_result(r, access_token)
            except Exception as e:
                r = SyncResult(r.naver_product_id, r.name, ACTION_ERROR,
                               f"{r.detail} → 반영 실패: {type(e).__name__} {str(e)[:80]}")
        results.append(r)
    return results


def print_results(results: List[SyncResult], dry_run: bool = True) -> None:
    if not results:
        print("등록된 상품이 없습니다.")
        return

    print(f"\n{'═' * 70}")
    print(f"  등록 상품 동기화 {'(미리보기 — 반영 안 함)' if dry_run else '(반영 완료)'}")
    print(f"{'═' * 70}")
    for r in results:
        print("  " + r.line())

    need = [r for r in results if r.action != ACTION_OK]
    print(f"{'─' * 70}")
    if need:
        print(f"  조치 필요 {len(need)}건 / 전체 {len(results)}건")
        if dry_run:
            print("  실제로 반영하려면 --apply 옵션을 사용하세요.")
    else:
        print(f"  전체 {len(results)}건 이상 없음")
    print()


def _demo() -> None:
    """실행 가능한 자체 점검 — 판정 분기만 검증 (네트워크 호출은 가짜로 대체)."""
    import types
    from unittest.mock import patch as _patch

    record = {"naver_product_id": "1", "name": "테스트", "domemae_goods_no": "111",
              "sale_price": 20_000, "supply_price": 10_000, "stock_quantity": 999}

    def fake(stock, supply_price):
        return types.SimpleNamespace(stock=stock, supply_price=supply_price, options=[])

    # 조회가 안 된 것을 "판매중지"로 판정하면 안 된다 — 연동이 끊긴 상태에서 일괄
    # 반영 한 번에 멀쩡한 상품이 전부 내려가 매출이 통째로 멈춘다.
    with _patch(f"{__name__}.fetch_product_detail", side_effect=ValueError("API 키 없음")):
        r = check_product(record)
        assert r.action == ACTION_ERROR, f"조회 실패를 {r.action}으로 판정 — 판매중지로 내리면 안 됨"

    # 도매처 재고는 판정과 무관하게 항상 담겨야 화면이 품절 임박을 미리 보여줄 수 있다.
    # (등록 수량보다 재고가 많아 조정할 게 없는 = 이상없음 상태)
    plenty = dict(record, stock_quantity=5)
    with _patch(f"{__name__}.fetch_product_detail", return_value=fake(7, 10_000)):
        r = check_product(plenty)
        assert r.action == ACTION_OK and r.supply_stock == 7, f"이상없음일 때 재고가 안 담김: {r}"

    with _patch(f"{__name__}.fetch_product_detail", return_value=fake(0, 10_000)):
        r = check_product(record)
        assert r.action == ACTION_SUSPEND and r.supply_stock == 0, "품절 판정/재고 오류"

    with _patch(f"{__name__}.fetch_product_detail", return_value=fake(3, 10_000)):
        r = check_product(record)
        assert r.action == ACTION_STOCK and r.supply_stock == 3, "재고조정 판정/재고 오류"

    print("sync self-check OK")


if __name__ == "__main__":
    _demo()
