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

from ..config import MIN_MARGIN
from ..margin.calculator import calculate
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
        # 도매매에서 상품이 내려갔거나 조회 실패 — 팔 수 없는 상태로 간주하고 내린다
        return SyncResult(pid, name, ACTION_SUSPEND,
                          f"도매매 조회 실패 ({type(e).__name__}) — 공급 중단 가능성, 판매중지")

    if p.stock <= 0:
        return SyncResult(pid, name, ACTION_SUSPEND, "도매매 품절 — 판매중지")

    # 도매가 변동 → 현재 판매가 기준으로 마진 재계산
    sale_price = int(record.get("sale_price", 0) or 0)
    old_cost = int(record.get("supply_price", 0) or 0)
    if sale_price and p.supply_price and p.supply_price != old_cost:
        m = calculate(sale_price=sale_price, cost_price=p.supply_price,
                      free_shipping=(sale_price >= 30_000))
        moved = p.supply_price - old_cost
        if not m.passes_min:
            return SyncResult(
                pid, name, ACTION_MARGIN_WARN,
                f"도매가 {old_cost:,}→{p.supply_price:,}원({moved:+,}) "
                f"마진 {m.margin_rate:.1%} < 최소 {MIN_MARGIN:.0%} — 판매가 재검토 필요",
            )
        return SyncResult(
            pid, name, ACTION_OK,
            f"도매가 {old_cost:,}→{p.supply_price:,}원({moved:+,}) 마진 {m.margin_rate:.1%} 유지",
        )

    registered_stock = int(record.get("stock_quantity", 0) or 0)
    if p.stock < registered_stock:
        return SyncResult(pid, name, ACTION_STOCK,
                          f"재고 {registered_stock:,}→{p.stock:,}개로 조정",
                          new_stock=p.stock)

    return SyncResult(pid, name, ACTION_OK, f"재고 {p.stock:,}개, 도매가 {p.supply_price:,}원")


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
