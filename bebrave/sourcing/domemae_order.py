"""
도매매 자동 발주 (Private API) — 로그인 + 주문서 생성.

⚠️ 매우 민감한 모듈: 도매매 계정 아이디/비밀번호를 사용해 로그인 세션(sId)을 발급받고,
사전 충전한 이머니로 실제 결제(발주)를 실행한다. 잘못 호출하면 실제 돈이 나간다.

⚠️ 네이버/카카오/애플 등 SNS로 가입한 계정은 로그인 API(setLogin) 사용 불가 —
공식 가이드가 "서드파티 쇼핑몰 1개당 별도의 도매꾹 사업자회원 아이디를 새로 생성"하도록
권장하므로, 자동발주 전용으로 아이디/비밀번호 방식의 신규 계정(가능하면 사업자인증)을
만들어 DOMEMAE_USER_ID/PW에 사용할 것 — 기존 SNS 연동 개인 계정과는 별개.

엔드포인트: https://domeggook.com/ssl/api/ (mode=setLogin, mode=setOrder ver 4.3)
공식 문서: 사용자 제공 "도매꾹_도매매_주문서_생성_API_연동_가이드_20260707.docx" (2026-07-12 확인, 검증됨)

환경변수:
  DOMEMAE_API_KEY      (기존 검색/조회에도 쓰는 키. 발주 전용 신규 계정으로 재발급 권장)
  DOMEMAE_USER_ID      (자동발주 전용 도매매 로그인 아이디 — SNS 연동 계정 불가)
  DOMEMAE_USER_PW      (위 계정의 비밀번호 — 유출 시 계정 전체 위험, 각별히 주의)
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE = "https://domeggook.com/ssl/api/"

STORE_NAME_MAX = 16  # 공식 가이드 deliinfo 8번 항목 최대 16글자(바로 위 추가연락처가 13글자 — 처음에 잘못 읽었다)


def store_name() -> str:
    """deliinfo의 "쇼핑몰명/상호명" — **내 스마트스토어 이름**(.env SELLER_STORE_NAME).

    공식 가이드(2026-07-07): "실제 소비자가 상품을 주문한 쇼핑몰의 이름을 입력합니다 … 도매꾹, 도매매 및
    그 하위 브랜드의 상표가 소비자에게 배송되는 박스나 송장에 노출되는 것을 막기 위한 것"(최대 16글자).
    2026-10까지 도매 상품명을 넣고 있었다 — 박스·송장에 내 가게 대신 긴 상품명이 찍히고 글자 수도 넘었다.
    미설정·길이 초과면 예외 — 엉뚱한 이름으로 조용히 발주되는 것보다 발주가 멈추는 게 낫다."""
    name = os.environ.get("SELLER_STORE_NAME", "").strip()
    if not name:
        raise ValueError("발주 전에 .env에 SELLER_STORE_NAME(내 스마트스토어 이름, 16글자 이내)을 넣어야 합니다 — "
                         "도매처가 박스·송장에 찍는 쇼핑몰명입니다")
    if len(name) > STORE_NAME_MAX:
        raise ValueError(f"SELLER_STORE_NAME '{name}'이 {len(name)}글자입니다 — 도매매 쇼핑몰명은 {STORE_NAME_MAX}글자까지입니다")
    return name


def format_phone(raw: str) -> str:
    """수령인 휴대전화를 도매매 형식(하이픈 포함)으로. 공식 가이드: 010~019, 050·0502~0507·0707 안심번호만
    휴대전화 칸에 들어가고, 유선전화(02·031 등)는 넣을 수 없다 — 형식이 틀리면 도매매가 주문을 거절한다.
    네이버가 하이픈 없이 줄 수도 있어 숫자만 뽑아 다시 맞춘다(실주문 형식 미검증, 2026-10)."""
    import re
    d = re.sub(r"\D", "", raw or "")
    if d.startswith(("0502", "0503", "0504", "0505", "0506", "0507", "0707")) and len(d) == 12:
        return f"{d[:4]}-{d[4:8]}-{d[8:]}"
    if d.startswith("050") and len(d) == 11:
        return f"{d[:3]}-{d[3:7]}-{d[7:]}"
    if re.match(r"01[0-9]", d) and len(d) in (10, 11):
        return f"{d[:3]}-{d[3:-4]}-{d[-4:]}"
    raise ValueError(f"수령인 연락처 '{raw}'가 휴대전화·안심번호가 아닙니다 — 도매매는 휴대전화만 받습니다. "
                     "고객에게 휴대전화를 확인해 수동 발주에서 고쳐 주세요")


def delivery_problem(zipcode: str, address1: str, address2: str, phone: str) -> str:
    """도매매가 거절할 배송지면 사람이 읽을 이유, 괜찮으면 빈 문자열. 발주 전(목록 표시)과 발주 직전에 같이 쓴다."""
    import re
    if not re.fullmatch(r"\d{5}", (zipcode or "").strip()):
        return "우편번호가 없거나 5자리가 아닙니다 — 주소 확인 필요"
    if not (address1 or "").strip():
        return "주소가 비어 있습니다 — 주소 확인 필요"
    if not (address2 or "").strip():
        return "상세주소(주소2)가 비어 있습니다 — 도매매 필수 칸. 상세주소가 없는 주소면 '없음'으로 적어 수동 발주"
    try:
        format_phone(phone)
    except ValueError as e:
        return str(e)
    return ""


@dataclass
class DeliveryInfo:
    """
    deliinfo 파라미터 (구분자 단일 |). 도매매 주문시 이 값은 '소비자(수령인) 주소'에만
    적용되고, 주문자 정보는 로그인한 회원(사업자)의 회원정보가 자동 입력됨.
    """
    name: str                      # 1. 성명 (필수)
    zipcode: str                   # 3. 우편번호 5자리 (필수)
    address1: str                  # 4. 주소1 (필수)
    address2: str                  # 5. 주소2 (필수)
    phone: str                     # 6. 휴대전화, 010-0000-0000 형식 (필수)
    shop_name: str                 # 8. 쇼핑몰명/상호명 — 내 스마트스토어 이름, 16글자 이내. store_name() 사용 (필수)
    email: str = ""                # 2. 이메일 (선택)
    phone2: str = ""               # 7. 추가연락처 (선택)
    customs_code: str = ""         # 9. 통관고유부호 (해외직배송 상품 주문시에만 필수)

    def to_field(self) -> str:
        return "|".join([
            self.name, self.email, self.zipcode, self.address1, self.address2,
            self.phone, self.phone2, self.shop_name, self.customs_code,
        ])


@dataclass
class OrderOption:
    """단일옵션 상품은 option_code='00'. 복수옵션은 여러 개를 리스트로 전달."""
    option_code: str = "00"
    quantity: int = 1


@dataclass
class OrderItem:
    goods_no: str
    market: str = "supply"                       # dome=도매꾹 / supply=도매매
    ship_payer: str = "S"                         # S=무료배송(판매자부담) / B=착불 / P=선결제
    options: List[OrderOption] = field(default_factory=lambda: [OrderOption()])
    seller_message: str = ""                      # 256자 이하
    delivery_message: str = ""                    # 256자 이하, 도매매 전용

    def to_field(self) -> str:
        option_str = "|".join(f"{o.option_code}|{o.quantity}" for o in self.options)
        # 5개 필드를 ||(더블 파이프)로 연결: 구매채널||배송비부담||옵션코드+개수||판매자전달사항||배송요청사항
        return f"{self.market}||{self.ship_payer}||{option_str}||{self.seller_message}||{self.delivery_message}"


def login(
    api_key: str = "",
    user_id: str = "",
    user_pw: str = "",
) -> dict:
    """
    도매매 로그인 → sId(세션값) 발급. SNS(네이버/카카오/애플) 가입 계정은 사용 불가.
    반환: {"sId": ..., "cId": ..., "grade": ...}
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    # aid는 반드시 id(발주 전용 계정)로 발급된 키여야 함 — 소싱용 DOMEMAE_API_KEY와 다름
    key = api_key or os.environ.get("DOMEMAE_ORDER_API_KEY", "") or os.environ.get("DOMEMAE_API_KEY", "")
    uid = user_id or os.environ.get("DOMEMAE_USER_ID", "")
    pw = user_pw or os.environ.get("DOMEMAE_USER_PW", "")
    if not (key and uid and pw):
        raise ValueError(".env 파일에 DOMEMAE_ORDER_API_KEY, DOMEMAE_USER_ID, DOMEMAE_USER_PW를 설정하세요.")

    resp = requests.post(_BASE, data={
        "ver": "4.1", "mode": "setLogin",
        "aid": key, "id": uid, "pw": pw,
        "om": "json", "loginKeep": "off",
        "device": "Third Party", "ip": "0:0:0:0:0:0:0:0",
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("domeggook", {})
    if data.get("result") != "true":
        raise RuntimeError(f"도매매 로그인 실패: {data}")
    return data


def fetch_emoney_balance(sId: str, api_key: str = "", user_id: str = "") -> dict:
    """
    도매매 이머니 잔액 조회 — 발주 대기열의 필요 금액과 대조해 이머니 부족을
    미리 경고하는 용도(2026-08 추가). 발주 실행 시점에 잔액 부족으로 실패하면
    발송 지연으로 이어지므로, 실행 전에 미리 아는 게 값어치가 있다.

    공식 문서: openapi.domeggook.com "회원자산내역조회"(mode=getMyAsset, ver 1.0, 2026-08 확인).
      POST https://www.domeggook.com/ssl/api/?ver=1.0&mode=getMyAsset&aid=&id=&sId=&om=json
    응답: domeggook.data.currEmoney.total(이머니 총합), .cash(현금성, 실사용 가능분),
      .card(카드성 — 미사용, 항상 0), domeggook.data.currPoint(포인트).

    Returns: {"total": int, "cash": int, "card": int, "point": int}
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    key = api_key or os.environ.get("DOMEMAE_ORDER_API_KEY", "") or os.environ.get("DOMEMAE_API_KEY", "")
    uid = user_id or os.environ.get("DOMEMAE_USER_ID", "")

    resp = requests.post(_BASE, data={
        "ver": "1.0", "mode": "getMyAsset",
        "aid": key, "id": uid, "sId": sId, "om": "json",
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("domeggook", {})
    # 실호출로 확인(2026-08): 이 엔드포인트는 성공시 result="SUCCESS"를 준다 —
    # login()/fetch_order_tracking()이 쓰는 "true"와 값이 다르다(엔드포인트마다 관례가 다름).
    if data.get("result") not in (None, "true", "SUCCESS"):
        raise RuntimeError(f"이머니 조회 실패: {data}")

    asset = data.get("data", {}) or {}
    emoney = asset.get("currEmoney", {}) or {}
    return {
        "total": int(emoney.get("total", 0) or 0),
        "cash": int(emoney.get("cash", 0) or 0),
        "card": int(emoney.get("card", 0) or 0),
        "point": int(asset.get("currPoint", 0) or 0),
    }


def fetch_order_tracking(order_no: str, sId: str, api_key: str = "", user_id: str = "") -> dict:
    """
    도매매 발주건의 배송 상태(택배사/송장번호) 조회.

    공식 문서: openapi.domeggook.com "구매 주문서 상세 조회"(mode=getOrderView, ver 4.1,
    2026-08 확인). place_order()의 setOrder API 가이드에는 없던 별도 엔드포인트다.
      GET https://www.domeggook.com/ssl/api/?ver=4.1&mode=getOrderView&aid=&id=&sId=&for=buy&no=&om=json
      no = 주문서번호에서 "OR" 접두어를 뺀 숫자만 — place_order()가 돌려주는
      order.orderNo("OR1234567" 형식)에서 파생.
    응답의 items.delivery 안에 companyName(택배사명)/company(코드)/code(송장번호)가
    온다고 문서에 나와 있으나, items가 단일객체인지 배열인지의 실제 JSON 예시는
    문서에 없어(설명 텍스트만 있음) 방어적으로 둘 다 처리한다 — 실주문으로 아직
    검증 안 됐으니 첫 호출 결과는 반드시 눈으로 확인할 것.

    Returns: {"company_name", "company_code", "tracking_number"} — 미확보시 빈 문자열.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    key = api_key or os.environ.get("DOMEMAE_ORDER_API_KEY", "") or os.environ.get("DOMEMAE_API_KEY", "")
    uid = user_id or os.environ.get("DOMEMAE_USER_ID", "")
    no = order_no[2:] if order_no.upper().startswith("OR") else order_no

    resp = requests.get(_BASE, params={
        "ver": "4.1", "mode": "getOrderView",
        "aid": key, "id": uid, "sId": sId,
        "for": "buy", "no": no, "om": "json",
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("domeggook", {})
    if data.get("result") not in (None, "true", "SUCCESS"):
        raise RuntimeError(f"도매매 주문조회 실패: {data}")

    items = data.get("items", {})
    item = items[0] if isinstance(items, list) and items else (items if isinstance(items, dict) else {})
    delivery = item.get("delivery", {}) or {}

    return {
        "company_name": delivery.get("companyName", ""),
        "company_code": delivery.get("company", ""),
        "tracking_number": delivery.get("code", ""),
    }


def place_order(
    items: List[OrderItem],
    delivery: DeliveryInfo,
    sId: str,
    api_key: str = "",
    user_id: str = "",
    receipt: bool = False,
    notify: bool = True,
    dry_run: bool = True,
) -> Optional[dict]:
    """
    주문서 생성 (실제 결제 — 사전 충전된 이머니 차감, "발송예정" 상태로 주문서 생성됨).

    dry_run=True(기본값): 실제 API 호출 없이 요청 파라미터만 출력.
    dry_run=False로 명시해야 진짜로 결제/발주가 실행됨.
    구매자(id)와 판매자가 동일하면 SELF_ORDER 오류 발생 — 도매꾹/도매매 자체에 상품을 등록해
    판매하지 않는 이상 해당 없음.
    """
    # 도매매가 거절할 배송지는 보내기 전에 막는다 — 실패 사유가 "확인 필요"에 그대로 남아 고칠 곳이 보인다
    problem = delivery_problem(delivery.zipcode, delivery.address1, delivery.address2, delivery.phone)
    if problem:
        raise ValueError(problem)
    delivery.phone = format_phone(delivery.phone)
    key = api_key or os.environ.get("DOMEMAE_ORDER_API_KEY", "") or os.environ.get("DOMEMAE_API_KEY", "")
    uid = user_id or os.environ.get("DOMEMAE_USER_ID", "")
    if not dry_run and not (key and uid):
        raise ValueError(".env 파일에 DOMEMAE_ORDER_API_KEY, DOMEMAE_USER_ID를 설정하세요.")
    key = key or "{DOMEMAE_ORDER_API_KEY 미설정}"
    uid = uid or "{DOMEMAE_USER_ID 미설정}"

    body = {
        "ver": "4.3", "mode": "setOrder",
        "aid": key, "id": uid, "sId": sId,
        "receipt": "1" if receipt else "0",
        "notify": "true" if notify else "false",
        "ie": "utf-8", "oe": "utf-8", "om": "json",
        "alliance": "bebrave",
    }
    for item in items:
        body[f"item[{item.goods_no}]"] = item.to_field()
    body["deliinfo"] = delivery.to_field()

    if dry_run:
        print("[dry-run] 실제 발주 안 함 — 아래 요청 내용만 확인:")
        for k, v in body.items():
            if k == "sId":
                continue
            print(f"  {k} = {v}")
        return None

    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.post(_BASE, data=body, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("domeggook", {})
    if data.get("result") != "SUCCESS":
        raise RuntimeError(f"발주 실패: {data}")
    return data


def _demo() -> None:
    """실행 가능한 자체 점검 — 연락처 형식·배송지 검사·상호명 길이 (네트워크 없음)."""
    assert format_phone("01012345678") == "010-1234-5678"
    assert format_phone("010-1234-5678") == "010-1234-5678"
    assert format_phone("0507-1234-5678") == "0507-1234-5678"
    assert format_phone("0161234567") == "016-123-4567"
    for bad in ("02-1234-5678", "031-123-4567", ""):
        try:
            format_phone(bad); raise AssertionError(f"유선·빈 번호가 통과됨: {bad}")
        except ValueError:
            pass
    assert delivery_problem("06000", "서울시 강남구", "101호", "01012345678") == ""
    assert "우편번호" in delivery_problem("", "경기도 화성시", "1층", "01012345678")
    assert "상세주소" in delivery_problem("06000", "서울시", " ", "01012345678")
    assert "휴대전화" in delivery_problem("06000", "서울시", "1층", "02-123-4567")
    os.environ["SELLER_STORE_NAME"] = "가" * 16; assert store_name() == "가" * 16
    os.environ["SELLER_STORE_NAME"] = "가" * 17
    try:
        store_name(); raise AssertionError("17글자 상호명이 통과됨")
    except ValueError:
        pass
    print("domemae_order self-check OK")


if __name__ == "__main__":
    _demo()
