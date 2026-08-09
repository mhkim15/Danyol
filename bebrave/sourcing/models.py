from dataclasses import dataclass, field
from datetime import date
from typing import Optional


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
    # 도매매 매칭 결과 (discover() 자동탐색 시 채워짐) — 화면에 그대로 노출해
    # "미리보기"가 별도로 재검색하며 매칭 확인 로직을 우회하던 문제를 없앤다(2026-08).
    supply_name: str = ""
    supply_goods_no: str = ""  # 도매매 상품코드 — 동일 상품 중복 제거용
    margin_rate: float = 0.0
    supply_matched: Optional[bool] = None  # True=형태 일치 확인, False=불확실, None=미조회
    # 자동판정(supply_matched)과 별개 — 사람이 실물/상세페이지를 보고 승인했는지
    # 기록만 남긴다. 점수·supply_matched는 건드리지 않는다(자동 신뢰도와 사람
    # 승인을 섞으면 나중에 뭐가 자동판정이고 뭐가 사람확인인지 못 구분하게 됨, 2026-08).
    human_confirmed: bool = False

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
            "supply_name": self.supply_name,
            "supply_goods_no": self.supply_goods_no,
            "margin_rate": self.margin_rate,
            "supply_matched": self.supply_matched,
            "human_confirmed": self.human_confirmed,
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
            supply_name=data.get("supply_name", ""),
            supply_goods_no=data.get("supply_goods_no", ""),
            margin_rate=data.get("margin_rate", 0.0),
            supply_matched=data.get("supply_matched"),
            human_confirmed=data.get("human_confirmed", False),
        )
