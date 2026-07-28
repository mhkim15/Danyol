from dataclasses import dataclass, field
from datetime import date


@dataclass
class ProductCandidate:
    keyword: str
    monthly_search: int
    product_count: int
    category: str
    is_seasonal: bool = False
    notes: str = ""
    added_date: str = field(default_factory=lambda: date.today().isoformat())
    # 멀티팩터 스코어 (0~100점, analyze() 호출 시 자동 계산)
    score: int = 0
    # 마진 검증용 가격 (선택 입력)
    est_sale_price: int = 0
    est_cost_price: int = 0

    @property
    def golden_ratio(self) -> float:
        if self.product_count == 0:
            return float("inf")
        return round(self.monthly_search / self.product_count, 2)

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "monthly_search": self.monthly_search,
            "product_count": self.product_count,
            "golden_ratio": self.golden_ratio,
            "category": self.category,
            "is_seasonal": self.is_seasonal,
            "notes": self.notes,
            "added_date": self.added_date,
            "score": self.score,
            "est_sale_price": self.est_sale_price,
            "est_cost_price": self.est_cost_price,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProductCandidate":
        return cls(
            keyword=data["keyword"],
            monthly_search=data["monthly_search"],
            product_count=data["product_count"],
            category=data["category"],
            is_seasonal=data.get("is_seasonal", False),
            notes=data.get("notes", ""),
            added_date=data.get("added_date", date.today().isoformat()),
            score=data.get("score", 0),
            est_sale_price=data.get("est_sale_price", 0),
            est_cost_price=data.get("est_cost_price", 0),
        )
