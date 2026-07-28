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
    shop_name: str                 # 8. 쇼핑몰명/상호명 — 도매꾹 상표 노출 방지용 (필수)
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
