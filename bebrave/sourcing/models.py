from dataclasses import dataclass, field
from datetime import date
from typing import Optional


def registration_block_reason(supply_matched: Optional[bool], human_confirmed: bool) -> str:
    """등록 버튼을 막을지 판정 — 빈 문자열이면 등록 가능.

    오매칭 대응은 자동 차단(supply_matched)과 사람 확인(human_confirmed)을
    둘 다 요구한다(2026-09 확정 방침) — 어느 한쪽만 통과해선 안 된다. 사람이
    확인했어도 자동판정이 여전히 불일치를 의심하면 막고, 자동판정이 맞다고
    나와도 사람이 실물을 안 봤으면 막는다.
    """
    if not human_confirmed:
        return "실물확인 미완료 — 미리보기에서 '실물확인 완료'를 눌러야 등록할 수 있습니다"
    if supply_matched is False:
        return "상품타입 불일치 의심 — 자동판정이 오매칭을 의심하고 있어 등록할 수 없습니다"
    return ""


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
    # 소싱 2트랙 구분 — "A"=신규 틈새(바로 등록) / "B"=리메이크 후보(손봐서 등록) / ""=미분류.
    # discover.py의 DiscoveryResult.track/recommendation을 그대로 옮겨온다(2026-08 복구 —
    # 전엔 to_product_candidates()가 이 값을 복사하지 않아 트랙이 저장 단계에서 사라졌었다).
    track: str = ""
    recommendation: str = ""
    # 도매매 상세설명 이미지 사용 허용 여부 — True만 목록에 보이고 등록된다. None=아직 확인 안 함.
    image_usable: Optional[bool] = None

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
            # product_count==0이면 golden_ratio는 수학적으로 무한대인데, float("inf")를
            # 그대로 json.dump하면 표준이 아닌 "Infinity" 리터럴이 파일에 박혀 다른 JSON
            # 파서가 못 읽는다(2026-09 발견). 화면 표시용 값이라 null로 직렬화하고, 실제
            # 계산은 로드 후 이 property로 다시 하면 된다(from_dict가 저장값을 안 씀).
            "golden_ratio": None if self.product_count == 0 else self.golden_ratio,
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
            "track": self.track,
            "recommendation": self.recommendation,
            "image_usable": self.image_usable,
        }

    @property
    def block_reason(self) -> str:
        return registration_block_reason(self.supply_matched, self.human_confirmed)

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
            track=data.get("track", ""),
            recommendation=data.get("recommendation", ""),
            image_usable=data.get("image_usable"),
        )
