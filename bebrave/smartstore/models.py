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
    supplier: str = ""                 # 공급사명
    keyword: str = ""                  # 소싱 키워드
    tags: List[str] = field(default_factory=list)  # 검색어 태그 (SEO)
    registered_date: str = field(default_factory=lambda: date.today().isoformat())
    naver_product_id: str = ""         # 등록 후 부여된 스마트스토어 상품 ID

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
        }

    def summary(self) -> str:
        return (
            f"[상품명] {self.name}\n"
            f"  판매가: {self.sale_price:,}원  도매가: {self.supply_price:,}원  마진: {self.margin_rate:.1%}\n"
            f"  카테고리ID: {self.leaf_category_id}  재고: {self.stock_quantity}개\n"
            f"  도매매: {self.domemae_goods_no}  공급사: {self.supplier}"
        )
