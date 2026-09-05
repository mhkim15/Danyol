"""
등록 항목 점검 — "스마트스토어에 실제로 뭐가 어떻게 등록됐는가"를 항목별로 판정한다.

기존엔 상품 상세 화면이 네이버 응답 전체를 받아놓고 5가지(이미지·태그·상세설명·재고·
상태)만 꺼내 쓰고 나머지를 버렸다. 원산지 코드가 뭐로 들어갔는지, A/S 번호가 더미인지,
고시 항목이 "상세페이지 참조"로 도배됐는지 확인할 방법이 화면에 없었다(2026-09).

이 모듈은 originProduct 구조(build_request_body()의 출력이든, fetch_registered_product()의
실시간 응답이든 같은 모양)를 받아 그룹별 항목 목록 + 판정을 돌려주는 순수 함수다.
네트워크 호출 없음 — 등록 전(dry-run 바디) / 등록 후(실시간 조회) 어느 쪽에도 그대로 쓴다.
"""
from dataclasses import dataclass
from typing import Optional

from ..config import SHIPPING_FEE, FREE_SHIPPING_THRESHOLD, RETURN_DELIVERY_FEE, EXCHANGE_DELIVERY_FEE

# register.py의 하드코딩과 정확히 같은 값이어야 "기본값"으로 판정할 수 있다 — 여기서
# 값이 바뀌면 register.py도 같이 바뀐 것인지 확인할 것.
_DUMMY_CS_PHONE = "010-0000-0000"
_DEFAULT_AS_GUIDE = "구매 후 문의사항은 고객센터로 연락 바랍니다."
_DEFAULT_DELIVERY_COMPANY = "CJGLS"
# 배송비 기준값은 리터럴로 다시 정의하지 않고 config.py에서 그대로 가져온다 — 예전엔
# 여기서 3000/30000/3000/6000을 따로 정의해서, config 값이 바뀌면 감사 패널만 옛
# 기준으로 "기본값" 판정을 계속 내리는 불일치가 있었다(2026-09).
_DEFAULT_BASE_FEE = SHIPPING_FEE
_DEFAULT_FREE_THRESHOLD = FREE_SHIPPING_THRESHOLD
_DEFAULT_RETURN_FEE = RETURN_DELIVERY_FEE
_DEFAULT_EXCHANGE_FEE = EXCHANGE_DELIVERY_FEE
_NOTICE_FALLBACK = "상세페이지 참조"

VERDICT_OK = "정상"
VERDICT_EMPTY = "비어 있음"
VERDICT_DEFAULT = "기본값"
VERDICT_DUMMY = "더미 의심"


@dataclass
class FieldAuditItem:
    group: str
    label: str
    value: str
    verdict: str
    note: str = ""
    # originProduct 안에서 이 항목이 실제로 들어가는 점(.) 경로 — 비어 있으면(옵션·이미지·
    # 고시 항목처럼 구조가 동적이거나 복합적인 항목) 화면에서 수정 입력칸을 만들지 않는다.
    # register._apply_field_overrides()가 이 경로 그대로 덮어쓴다(2026-09).
    field_path: str = ""

    @property
    def problem(self) -> bool:
        return self.verdict != VERDICT_OK

    @property
    def editable(self) -> bool:
        return bool(self.field_path)


def _item(group: str, label: str, value, verdict: str, note: str = "", field_path: str = "") -> FieldAuditItem:
    display = "" if value is None else str(value)
    return FieldAuditItem(group=group, label=label, value=display or "(없음)", verdict=verdict, note=note, field_path=field_path)


def _judge_present(group: str, label: str, value, note_if_empty: str = "", field_path: str = "") -> FieldAuditItem:
    if value:
        return _item(group, label, value, VERDICT_OK, field_path=field_path)
    return _item(group, label, "", VERDICT_EMPTY, note_if_empty, field_path=field_path)


def audit_fields(origin_product: dict, domemae_goods_no: str = "") -> list:
    """origin_product: build_request_body()가 만든 바디의 "originProduct" 값,
    또는 fetch_registered_product() 응답의 "originProduct" 값 — 둘 다 같은 모양.
    domemae_goods_no: 모델명이 도매매 상품번호로 새는 걸 잡기 위한 대조값(선택)."""
    items = []
    op = origin_product or {}
    detail = op.get("detailAttribute", {}) or {}

    # ── 기본정보 ──────────────────────────────────────────────────────
    g = "기본정보"
    items.append(_judge_present(g, "상품명", op.get("name")))
    items.append(_judge_present(g, "카테고리ID", op.get("leafCategoryId")))
    items.append(_item(g, "판매상태", op.get("statusType") or "", VERDICT_OK if op.get("statusType") else VERDICT_EMPTY))
    seller_code = (detail.get("sellerCodeInfo", {}) or {}).get("sellerManagementCode", "")
    items.append(_judge_present(
        g, "판매자상품코드", seller_code,
        "도매매 상품번호를 넣으면 발주 자동매칭의 키로 쓸 수 있는데 비어 있습니다.",
        field_path="detailAttribute.sellerCodeInfo.sellerManagementCode",
    ))

    # ── 가격·재고 ─────────────────────────────────────────────────────
    g = "가격·재고"
    items.append(_judge_present(g, "판매가", op.get("salePrice")))
    stock = op.get("stockQuantity")
    items.append(_item(g, "재고", stock if stock is not None else "", VERDICT_OK if stock is not None else VERDICT_EMPTY))

    # ── 이미지 ────────────────────────────────────────────────────────
    g = "이미지"
    images = op.get("images", {}) or {}
    rep = (images.get("representativeImage") or {}).get("url", "")
    items.append(_judge_present(g, "대표이미지", rep))
    optional = [i.get("url") for i in (images.get("optionalImages") or []) if i.get("url")]
    items.append(_item(
        g, "추가이미지", f"{len(optional)}장",
        VERDICT_OK if optional else VERDICT_EMPTY,
        "네이버는 9장까지 받는데 3장 이하면 등록 파이프라인이 그만큼만 올렸을 수 있습니다." if len(optional) < 4 else "",
    ))

    # ── 옵션 ──────────────────────────────────────────────────────────
    g = "옵션"
    option_info = detail.get("optionInfo") or op.get("optionInfo")
    if option_info:
        loc = "detailAttribute 안" if detail.get("optionInfo") else "originProduct 최상위(위치 오류 의심)"
        combos = (option_info.get("optionCombinations") or [])
        items.append(_item(
            g, "옵션 구성", f"{len(combos)}개 옵션 — {loc}",
            VERDICT_OK if detail.get("optionInfo") else VERDICT_DUMMY,
            "" if detail.get("optionInfo") else "네이버 스펙상 optionInfo는 detailAttribute 아래여야 합니다 — 이 위치면 옵션이 무시될 수 있습니다.",
        ))
    else:
        items.append(_item(g, "옵션 구성", "", VERDICT_EMPTY, "옵션 없는 단일상품이면 정상입니다."))

    # ── 배송 ──────────────────────────────────────────────────────────
    g = "배송"
    delivery = op.get("deliveryInfo", {}) or {}
    company = delivery.get("deliveryCompany", "")
    items.append(_item(
        g, "택배사", company,
        VERDICT_DEFAULT if company == _DEFAULT_DELIVERY_COMPANY else (VERDICT_OK if company else VERDICT_EMPTY),
        "전 상품 동일 택배사로 고정돼 있습니다 — 실제 출고 택배사와 다를 수 있습니다." if company == _DEFAULT_DELIVERY_COMPANY else "",
        field_path="deliveryInfo.deliveryCompany",
    ))
    fee = delivery.get("deliveryFee", {}) or {}
    base_fee = fee.get("baseFee")
    free_threshold = fee.get("freeConditionalAmount")
    is_default_fee = base_fee == _DEFAULT_BASE_FEE and free_threshold == _DEFAULT_FREE_THRESHOLD
    items.append(_item(
        g, "배송비", f"기본 {base_fee:,}원 / 무료기준 {free_threshold:,}원" if base_fee is not None else "",
        VERDICT_DEFAULT if is_default_fee else (VERDICT_OK if base_fee is not None else VERDICT_EMPTY),
        "전 상품 동일 값입니다 — 실제 공급사 배송비와 다르면 마진 계산과 어긋납니다." if is_default_fee else "",
    ))

    # ── 반품·교환 ─────────────────────────────────────────────────────
    g = "반품·교환"
    claim = delivery.get("claimDeliveryInfo", {}) or {}
    return_fee = claim.get("returnDeliveryFee")
    exchange_fee = claim.get("exchangeDeliveryFee")
    is_default_claim = return_fee == _DEFAULT_RETURN_FEE and exchange_fee == _DEFAULT_EXCHANGE_FEE
    items.append(_item(
        g, "반품/교환 배송비", f"반품 {return_fee:,}원 / 교환 {exchange_fee:,}원" if return_fee is not None else "",
        VERDICT_DEFAULT if is_default_claim else (VERDICT_OK if return_fee is not None else VERDICT_EMPTY),
        "전 상품 동일 값입니다." if is_default_claim else "",
    ))
    # 등록 요청에 주소록ID를 안 실어도 네이버가 스마트스토어센터에 등록된 주소록
    # (상품출고지·반품교환지)을 자동 배정한다 — 실제 등록 상품 조회로 반영 확인됨
    # (2026-09). 그동안 이 두 항목이 항상 "비어 있음"으로 떠서 진짜 문제(더미 A/S
    # 번호 등)를 가리는 오탐이었다.
    items.append(_item(
        g, "출고지 주소록ID", claim.get("shippingAddressId") or "(자동 배정)",
        VERDICT_OK, "" if claim.get("shippingAddressId") else "요청에 값을 안 실어도 스마트스토어센터 주소록에서 자동 배정됩니다.",
    ))
    items.append(_item(
        g, "반품지 주소록ID", claim.get("returnAddressId") or "(자동 배정)",
        VERDICT_OK, "" if claim.get("returnAddressId") else "요청에 값을 안 실어도 스마트스토어센터 주소록에서 자동 배정됩니다.",
    ))

    # ── 원산지·제조 ───────────────────────────────────────────────────
    g = "원산지·제조"
    origin = detail.get("originAreaInfo", {}) or {}
    origin_code = origin.get("originAreaCode", "")
    items.append(_judge_present(g, "원산지 코드", origin_code, field_path="detailAttribute.originAreaInfo.originAreaCode"))
    items.append(_judge_present(
        g, "제조사(manufacturerName)", detail.get("manufacturerName"),
        "값이 없으면 네이버쇼핑 가격비교 매칭에 불리합니다.",
        field_path="detailAttribute.manufacturerName",
    ))
    items.append(_judge_present(g, "브랜드(brandName)", detail.get("brandName"), field_path="detailAttribute.brandName"))
    model_name = detail.get("modelName", "")
    is_goods_no_leak = bool(domemae_goods_no) and model_name == domemae_goods_no
    items.append(_item(
        g, "모델명(modelName)", model_name,
        VERDICT_DUMMY if is_goods_no_leak else (VERDICT_OK if model_name else VERDICT_EMPTY),
        "도매매 상품번호가 그대로 모델명에 들어가 공급사 내부번호가 노출됩니다." if is_goods_no_leak else "",
        field_path="detailAttribute.modelName",
    ))

    # ── A/S ───────────────────────────────────────────────────────────
    g = "A/S"
    as_info = detail.get("afterServiceInfo", {}) or {}
    phone = as_info.get("afterServiceTelephoneNumber", "")
    items.append(_item(
        g, "A/S 전화번호", phone,
        VERDICT_DUMMY if phone == _DUMMY_CS_PHONE else (VERDICT_OK if phone else VERDICT_EMPTY),
        "실제 연락처가 아닌 자리표시자입니다 — 구매자가 문의할 방법이 없습니다." if phone == _DUMMY_CS_PHONE else "",
        field_path="detailAttribute.afterServiceInfo.afterServiceTelephoneNumber",
    ))
    guide = as_info.get("afterServiceGuideContent", "")
    items.append(_item(
        g, "A/S 안내문구", guide,
        VERDICT_DEFAULT if guide == _DEFAULT_AS_GUIDE else (VERDICT_OK if guide else VERDICT_EMPTY),
        "전 상품 동일 문구입니다." if guide == _DEFAULT_AS_GUIDE else "",
        field_path="detailAttribute.afterServiceInfo.afterServiceGuideContent",
    ))

    # ── 상품정보제공고시 ──────────────────────────────────────────────
    g = "상품정보제공고시"
    notice = detail.get("productInfoProvidedNotice", {}) or {}
    notice_type = notice.get("productInfoProvidedNoticeType", "")
    items.append(_item(
        g, "고시유형", notice_type,
        VERDICT_DEFAULT if notice_type == "ETC" else (VERDICT_OK if notice_type else VERDICT_EMPTY),
        "구체적인 유형을 못 찾아 기타로 떨어졌습니다 — 소재·치수 같은 필수 항목이 빠질 수 있습니다." if notice_type == "ETC" else "",
    ))
    notice_fields = next((v for k, v in notice.items() if k != "productInfoProvidedNoticeType" and isinstance(v, dict)), {})

    def _is_goods_no_leak(field_name: str, value) -> bool:
        # 고시 안쪽 modelName도 detailAttribute.modelName과 같은 유출 경로다 —
        # register.py가 한때 도매매 상품번호로 폴백해 공급사 내부번호가 고시에
        # 노출됐다(2026-09). 두 자리 모두 같은 기준으로 잡는다.
        return field_name == "modelName" and bool(domemae_goods_no) and value == domemae_goods_no

    problem_count = sum(
        1 for k, v in notice_fields.items() if v == _NOTICE_FALLBACK or _is_goods_no_leak(k, v)
    )
    for field_name, value in notice_fields.items():
        is_fallback = value == _NOTICE_FALLBACK
        is_goods_no_leak_here = _is_goods_no_leak(field_name, value)
        items.append(_item(
            g, f"고시 · {field_name}", value,
            VERDICT_DUMMY if (is_fallback or is_goods_no_leak_here) else (VERDICT_OK if value else VERDICT_EMPTY),
            "도매매 상품번호가 그대로 노출됩니다." if is_goods_no_leak_here else
            ("실제 값을 못 찾아 자리표시자로 채워졌습니다." if is_fallback else ""),
        ))
    if notice_fields:
        items.append(_item(
            g, "고시 항목 요약", f"{len(notice_fields) - problem_count}/{len(notice_fields)}개 항목에 실제 값",
            VERDICT_OK if problem_count == 0 else VERDICT_DUMMY,
        ))

    # ── 검색·노출 ─────────────────────────────────────────────────────
    g = "검색·노출"
    tags = [t.get("text", "") if isinstance(t, dict) else str(t)
            for t in (detail.get("seoInfo", {}) or {}).get("sellerTags") or []]
    items.append(_item(g, "검색어 태그", f"{len(tags)}개 — {', '.join(tags)}" if tags else "",
                        VERDICT_OK if tags else VERDICT_EMPTY))
    minor = detail.get("minorPurchasable")
    items.append(_item(g, "미성년자구매", "가능" if minor else "불가", VERDICT_DEFAULT, "전 상품 True로 고정돼 있습니다." if minor else ""))
    benefit = (op.get("customerBenefit", {}) or {}).get("immediateDiscountPolicy")
    items.append(_judge_present(g, "즉시할인", "설정됨" if benefit else "", "즉시할인이 없으면 목록에서 할인 뱃지가 안 붙습니다." if not benefit else ""))

    return items


def _demo() -> None:
    """실행 가능한 자체 점검 — 정상/비어있음/기본값/더미의심 네 판정이 실제로 갈리는지
    확인 (네트워크 호출 없음)."""
    body_missing = {
        "name": "실리콘주걱", "leafCategoryId": "50000803", "salePrice": 8200, "stockQuantity": 999,
        "images": {"representativeImage": {"url": "https://x/a.jpg"}, "optionalImages": []},
        "deliveryInfo": {
            "deliveryCompany": "CJGLS",
            "deliveryFee": {"baseFee": 3000, "freeConditionalAmount": 30000},
            "claimDeliveryInfo": {"returnDeliveryFee": 3000, "exchangeDeliveryFee": 6000},
        },
        "detailAttribute": {
            "afterServiceInfo": {"afterServiceTelephoneNumber": "010-0000-0000",
                                  "afterServiceGuideContent": "구매 후 문의사항은 고객센터로 연락 바랍니다."},
            "originAreaInfo": {"originAreaCode": "0200037"},
            "modelName": "11013443",
            "productInfoProvidedNotice": {"productInfoProvidedNoticeType": "ETC",
                                           "etc": {"material": "상세페이지 참조", "modelName": "11013443"}},
            "seoInfo": {"sellerTags": [{"text": "실리콘주걱"}]},
            "minorPurchasable": True,
        },
    }
    items = audit_fields(body_missing, domemae_goods_no="11013443")
    by_label = {i.label: i for i in items}

    assert by_label["A/S 전화번호"].verdict == VERDICT_DUMMY, "더미 전화번호를 못 잡음"
    assert by_label["택배사"].verdict == VERDICT_DEFAULT, "하드코딩 택배사를 못 잡음"
    assert by_label["모델명(modelName)"].verdict == VERDICT_DUMMY, "도매매 상품번호 유출을 못 잡음"
    assert by_label["제조사(manufacturerName)"].verdict == VERDICT_EMPTY, "빈 제조사를 못 잡음"
    assert by_label["판매자상품코드"].verdict == VERDICT_EMPTY, "빈 판매자상품코드를 못 잡음"
    assert by_label["고시 · material"].verdict == VERDICT_DUMMY, "고시 폴백값을 못 잡음"
    assert by_label["고시 · modelName"].verdict == VERDICT_DUMMY, "고시 안쪽 도매매 상품번호 유출을 못 잡음"
    assert by_label["고시 항목 요약"].value == "0/2개 항목에 실제 값", "고시 유출/폴백을 요약 집계에서 놓침"
    assert by_label["옵션 구성"].verdict == VERDICT_EMPTY, "옵션 없는 상품을 잘못 판정"

    body_ok = dict(body_missing)
    body_ok["detailAttribute"] = dict(body_missing["detailAttribute"])
    body_ok["detailAttribute"]["afterServiceInfo"] = {
        "afterServiceTelephoneNumber": "010-1234-5678", "afterServiceGuideContent": "직접 작성한 안내",
    }
    items_ok = audit_fields(body_ok)
    by_label_ok = {i.label: i for i in items_ok}
    assert by_label_ok["A/S 전화번호"].verdict == VERDICT_OK, "정상 전화번호를 오판정"

    # 문제 있는 항목은 수정 입력칸을 만들 수 있어야 하고(field_path 있음), 옵션·이미지처럼
    # 구조가 동적인 항목은 수정칸을 안 만들어야 한다(2026-09 — "점검만 하고 못 고친다"는
    # 지적으로 추가).
    assert by_label["A/S 전화번호"].editable, "더미 전화번호인데 수정 입력칸을 못 만듦"
    assert by_label["판매자상품코드"].editable
    assert by_label["제조사(manufacturerName)"].editable
    assert by_label["A/S 전화번호"].field_path == "detailAttribute.afterServiceInfo.afterServiceTelephoneNumber"
    assert not by_label["옵션 구성"].editable, "구조가 동적인 옵션 항목에 수정칸이 생김"

    print("field_audit._demo self-check OK")


if __name__ == "__main__":
    _demo()
