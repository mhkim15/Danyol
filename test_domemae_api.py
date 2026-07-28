"""
도매매 API 연동 테스트 — 수정된 domemae.py 검증
"""
import sys
sys.path.insert(0, "/Users/danyol/Desktop/비브레이브")

import os
os.environ["DOMEMAE_API_KEY"] = "f529986ba5dd60846a6a9dd65aaf55f9"

from bebrave.sourcing.domemae import search_products, fetch_product_detail

print("=" * 60)
print("1. 키워드 검색 — 실리콘주걱")
print("=" * 60)
result = search_products("실리콘주걱", limit=3)
print(f"총 {result.total}개 결과")
for p in result.products:
    print(f"  [{p.goods_no}] {p.name[:35]}")
    print(f"    도매가: {p.supply_price:,}원 | 최소주문: {p.min_order_qty}개 | 공급사: {p.supplier}")

if result.cheapest:
    print(f"\n최저가 상품: {result.cheapest.name[:40]}")
    print(f"  도매가: {result.cheapest.supply_price:,}원")
    print(f"  상품번호: {result.cheapest.goods_no}")

print("\n" + "=" * 60)
print("2. 상품 상세 조회 — getItemView")
print("=" * 60)
if result.cheapest:
    detail = fetch_product_detail(result.cheapest.goods_no)
    print(f"상품명: {detail.name}")
    print(f"도매가: {detail.supply_price:,}원")
    print(f"재고: {detail.stock:,}개")
    print(f"배송비: {detail.shipping_fee:,}원")
    print(f"카테고리: {detail.category}")
    print(f"이미지: {detail.images[:2]}")
    print(f"상세설명 길이: {len(detail.description)}자")

print("\n완료")
