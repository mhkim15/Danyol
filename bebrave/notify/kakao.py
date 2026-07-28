"""
카카오톡 "나에게 보내기" API — 사업자등록/채널심사 없이 개인 계정으로 자기 자신에게
메시지 발송. 알림톡(비즈니스 메시지)과는 완전히 다른, 카카오 로그인 기반 API.

사전 준비 (사용자가 1회만 직접 수행— Claude가 대신 로그인할 수 없음):
  1. https://developers.kakao.com 에서 애플리케이션 생성 (개인, 즉시 생성됨)
  2. 앱 설정 > 카카오 로그인 활성화, Redirect URI 등록 (예: https://localhost)
  3. 앱 설정 > 카카오 로그인 > 동의항목에서 "카카오톡 메시지 전송(talk_message)" 활성화
  4. 아래 URL을 브라우저로 열어 로그인/동의 → redirect_uri로 리다이렉트된 URL에서 code= 파라미터 복사:
     https://kauth.kakao.com/oauth/authorize?client_id={REST_API_KEY}&redirect_uri={REDIRECT_URI}&response_type=code&scope=talk_message
  5. `exchange_code_for_tokens()` 로 1회 교환 → 반환된 refresh_token을 .env의 KAKAO_REFRESH_TOKEN에 저장

이후로는 refresh_token으로 access_token을 자동 갱신하며 발송 (사용할 때마다 refresh_token도 갱신되어
2개월 이상 계속 만료 없이 사용 가능 — 매일 도는 예약작업이 자연스럽게 갱신을 유지함).

환경변수:
  KAKAO_REST_API_KEY
  KAKAO_REFRESH_TOKEN  (최초 1회 발급 후 저장, 이후 자동 갱신되면 .env에 재기록 필요)
"""
import os
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


def build_authorize_url(rest_api_key: str, redirect_uri: str) -> str:
    """최초 1회, 사용자가 브라우저에서 직접 열어 로그인/동의할 URL."""
    return (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={rest_api_key}&redirect_uri={redirect_uri}"
        "&response_type=code&scope=talk_message"
    )


def exchange_code_for_tokens(
    code: str,
    rest_api_key: str,
    redirect_uri: str,
) -> dict:
    """
    authorize 후 리다이렉트로 받은 code를 access_token/refresh_token으로 교환 (최초 1회).
    반환값의 refresh_token을 .env의 KAKAO_REFRESH_TOKEN에 저장할 것.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(rest_api_key: str, refresh_token: str) -> dict:
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_to_me(
    text: str,
    button_title: str = "",
    button_url: str = "",
    rest_api_key: str = "",
    refresh_token: str = "",
) -> None:
    """
    나에게 카카오톡 메시지 발송. 텍스트 + 선택적 웹링크 버튼 1개.
    refresh_token으로 매번 access_token을 새로 받아 쓰므로 별도 캐시 불필요
    (호출 빈도가 하루 몇 번 수준이라 매번 갱신해도 무리 없음).

    반환된 새 refresh_token은 카카오 정책상 갱신될 수 있음 — 콘솔에 출력하니
    .env의 KAKAO_REFRESH_TOKEN이 바뀌었다면 갱신해줄 것.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    key = rest_api_key or os.environ.get("KAKAO_REST_API_KEY", "")
    rtoken = refresh_token or os.environ.get("KAKAO_REFRESH_TOKEN", "")
    if not key or not rtoken:
        raise ValueError(".env 파일에 KAKAO_REST_API_KEY, KAKAO_REFRESH_TOKEN을 설정하세요.")

    tokens = _refresh_access_token(key, rtoken)
    access_token = tokens["access_token"]
    if tokens.get("refresh_token") and tokens["refresh_token"] != rtoken:
        print(f"[알림] 카카오 refresh_token이 갱신됨 — .env의 KAKAO_REFRESH_TOKEN을 아래 값으로 교체하세요:\n  {tokens['refresh_token']}")

    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": button_url or "https://smartstore.naver.com", "mobile_web_url": button_url or "https://smartstore.naver.com"},
    }
    if button_title:
        template["button_title"] = button_title

    import json as _json
    resp = requests.post(
        _SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": _json.dumps(template, ensure_ascii=False)},
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"카카오톡 발송 실패 [{resp.status_code}]: {resp.text[:300]}")
