"""
등록 상품 트래커 — 미판매 상품 감지 및 자동삭제 경고.

판매 현황(last_sold_date)은 `main.py orders check`로 조회된 주문 데이터를 기반으로
`sync_from_orders()`가 갱신한다 (스마트스토어 주문 API가 원천 데이터).
"""
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Union

from ..config import STALE_PRODUCT_MONTHS, AUTO_DELETE_MONTHS


@dataclass
class TrackedProduct:
    product_id: str
    name: str
    registered_date: str
    last_sold_date: Optional[str] = None
    notes: str = ""

    def months_since_sold(self, today: Optional[date] = None) -> Optional[int]:
        ref = today or date.today()
        if self.last_sold_date is None:
            reg = date.fromisoformat(self.registered_date)
            return (ref - reg).days // 30
        return (ref - date.fromisoformat(self.last_sold_date)).days // 30

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "registered_date": self.registered_date,
            "last_sold_date": self.last_sold_date,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackedProduct":
        return cls(**data)


class ProductTracker:
    def __init__(self, data_path: Union[str, Path]):
        self.path = Path(data_path)
        self.products: list[TrackedProduct] = self._load()

    def _load(self) -> list[TrackedProduct]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [TrackedProduct.from_dict(d) for d in json.load(f)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([p.to_dict() for p in self.products], f, ensure_ascii=False, indent=2)

    def stale_products(self) -> list[TrackedProduct]:
        """3개월 이상 미판매 상품 반환."""
        return [p for p in self.products if (p.months_since_sold() or 0) >= STALE_PRODUCT_MONTHS]

    def auto_delete_risk(self) -> list[TrackedProduct]:
        """13개월 자동삭제 위험 상품 반환."""
        return [p for p in self.products if (p.months_since_sold() or 0) >= AUTO_DELETE_MONTHS]

    def add_or_update(self, product_id: str, name: str, registered_date: str) -> TrackedProduct:
        for p in self.products:
            if p.product_id == product_id:
                return p
        p = TrackedProduct(product_id=product_id, name=name, registered_date=registered_date)
        self.products.append(p)
        return p

    def sync_from_orders(self, orders) -> int:
        """
        `bebrave.smartstore.orders.ProductOrder` 리스트를 받아 판매된 상품의
        last_sold_date를 오늘 날짜로 갱신. 갱신된 상품 수를 반환.

        주문의 product_name으로 매칭 (스마트스토어 주문 API는 productOrderId만 주고
        원본 소싱 상품과 연결할 명시적 키가 없어, 이름 매칭이 현재로선 가장 안전한 방법).
        """
        today = date.today().isoformat()
        updated = 0
        for order in orders:
            for p in self.products:
                if p.name and p.name in order.product_name:
                    p.last_sold_date = today
                    updated += 1
        return updated
