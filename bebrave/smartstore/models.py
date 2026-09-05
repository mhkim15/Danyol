"""
스마트스토어 상품 등록 데이터 모델.
도매매 상품 정보 → 커머스 API 요청 구조로 변환.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import List


@dataclass
class StoreProduct:
    name: str                          # SEO 최적화 상품명 (30자 이내 권장)
    leaf_category_id: str              # 스마트스토어 카테고리 ID (말단)
    sale_price: int                    # 판매가 (원)
    stock_quantity: int                # 재고 수량
    detail_content: str                # 상세설명 HTML
    representative_image: str          # 대표 이미지 URL
    optional_images: List[str] = field(default_factory=list)  # 추가 이미지
    supply_price: int = 0              # 도매가 (내부 기록, 등록 요청에 미포함)
    margin_rate: float = 0.0           # 계산된 마진율
    domemae_goods_no: str = ""         # 도매매 상품번호 (추적용)
    domemae_category: str = ""         # 도매매 카테고리 경로 — 상품정보제공고시 유형 결정에 사용
    supplier: str = ""                 # 공급사명
    keyword: str = ""                  # 소싱 키워드
    tags: List[str] = field(default_factory=list)  # 검색어 태그 (SEO)
    origin_country: str = ""           # 원산지 (도매매 원본값, 빈 값이면 미확인)
    origin_code: str = ""              # 네이버 원산지 코드 (origin.resolve_origin_code 결과) — 빈 값이면 등록 불가
    manufacturer: str = ""             # 제조사 (도매매 detail.manufacturer) — 빈 값이면 미확인
    model: str = ""                    # 제조사 모델명 (도매매 detail.model) — 빈 값이면 미확인
    option_group_name: str = ""        # 옵션 축 이름 (예: "색상") — 빈 값이면 옵션 없음
    options: List[dict] = field(default_factory=list)  # [{"name","extra_price","stock"}]
    registered_date: str = field(default_factory=lambda: date.today().isoformat())
    naver_product_id: str = ""         # 등록 후 부여된 스마트스토어 상품 ID
    discount_rate: float = 0.0         # 즉시할인율 (0~1). 0이면 할인 없음 — sale_price는 항상
                                        # 할인 전 정가이고, 마진 게이트는 파이프라인에서 할인
                                        # 적용 후 가격 기준으로 별도 확인한다(2026-09)
    field_overrides: dict = field(default_factory=dict)
    # "등록 항목 점검" 패널에서 문제로 잡힌 값(더미 A/S 번호·빈 제조사 등)을 그 자리에서
    # 바로 고쳐 등록에 반영하기 위한 범용 통로(2026-09). 점검만 되고 못 고친다는 지적으로
    # 추가 — key는 originProduct 안에서의 점(.) 경로(예: "detailAttribute.brandName"),
    # value는 사용자가 입력한 새 값. register.build_request_body()가 최종 바디에 덮어쓴다.

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "leaf_category_id": self.leaf_category_id,
            "sale_price": self.sale_price,
            "stock_quantity": self.stock_quantity,
            "supply_price": self.supply_price,
            "margin_rate": round(self.margin_rate, 4),
            "domemae_goods_no": self.domemae_goods_no,
            "supplier": self.supplier,
            "keyword": self.keyword,
            "registered_date": self.registered_date,
            "naver_product_id": self.naver_product_id,
            # 발주 자동 매칭에 필요 — 옵션 상품은 option_group_name/options(code 포함)가
            # 없으면 어떤 도매매 옵션코드로 발주해야 할지 알 수 없다 (2026-08 추가).
            "option_group_name": self.option_group_name,
            "options": self.options,
        }

    def summary(self) -> str:
        return (
            f"[상품명] {self.name}\n"
            f"  판매가: {self.sale_price:,}원  도매가: {self.supply_price:,}원  마진: {self.margin_rate:.1%}\n"
            f"  카테고리ID: {self.leaf_category_id}  재고: {self.stock_quantity}개\n"
            f"  도매매: {self.domemae_goods_no}  공급사: {self.supplier}"
        )
