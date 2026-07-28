"""
네이버 커머스 API OAuth 2.0 인증.

인증 방식 (2026-07 확인 — 검색광고 API와 다름, bcrypt 기반):
  POST https://api.commerce.naver.com/external/v1/oauth2/token
  message = client_id + "_" + timestamp
  client_secret_sign = base64(bcrypt.hashpw(message, salt=client_secret))
  (client_secret 자체가 "$2a$04$..." 형식의 bcrypt salt로 발급됨)

환경변수:
  NAVER_COMMERCE_CLIENT_ID
  NAVER_COMMERCE_CLIENT_SECRET
"""
import base64
import os
import time
from typing import Optional

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_TOKEN_URL = "https://api.commerce.naver.com/external/v1/oauth2/token"

# 메모리 토큰 캐시
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0


def get_access_token(
    client_id: str = "",
    client_secret: str = "",
) -> str:
    """
    커머스 API 액세스 토큰 반환 (캐시된 토큰이 유효하면 재사용).
    """
    global _cached_token, _token_expires_at

    cid = client_id or os.environ.get("NAVER_COMMERCE_CLIENT_ID", "")
    csecret = client_secret or os.environ.get("NAVER_COMMERCE_CLIENT_SECRET", "")
    if not cid or not csecret:
        raise ValueError(
            ".env 파일에 NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET을 설정하세요."
        )

    # 캐시 유효 확인 (만료 60초 전에 갱신)
    if _cached_token and time.time() < _token_expires_at - 60:
        return _cached_token

    _cached_token = _fetch_token(cid, csecret)
    _token_expires_at = time.time() + 3600  # 1시간 유효
    return _cached_token


def _fetch_token(client_id: str, client_secret: str) -> str:
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    timestamp = str(int(time.time() * 1000))
    sign = _make_client_secret_sign(timestamp, client_id, client_secret)

    data = {
        "client_id": client_id,
        "timestamp": timestamp,
        "client_secret_sign": sign,
        "grant_type": "client_credentials",
        "type": "SELF",
    }
    resp = requests.post(_TOKEN_URL, data=data, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    token = result.get("access_token", "")
    if not token:
        raise ValueError(f"토큰 발급 실패: {result}")
    return token


def _make_client_secret_sign(timestamp: str, client_id: str, client_secret: str) -> str:
    """
    커머스 API 클라이언트 시크릿 서명 생성 (bcrypt).
    메시지: client_id + "_" + timestamp
    salt : client_secret ("$2a$04$..." 형식)
    """
    if not _HAS_BCRYPT:
        raise NotImplementedError("pip3 install bcrypt 후 재시도하세요.")

    message = f"{client_id}_{timestamp}".encode("utf-8")
    hashed = bcrypt.hashpw(message, client_secret.encode("utf-8"))
    return base64.b64encode(hashed).decode("utf-8")
