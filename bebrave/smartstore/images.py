"""
네이버 커머스 API 이미지 업로드.

상품 등록시 이미지 URL은 네이버 자체 이미지 서버(shop-phinf.pstatic.net 등)에
업로드된 URL만 허용됨 — 외부(도매매 등) CDN URL은 InvalidImageUrl 오류 발생
(2026-07-12 실전 테스트로 확인).

API: POST https://api.commerce.naver.com/external/v1/product-images/upload
Content-Type: multipart/form-data, 필드명 imageFiles (최대 10개)
"""
import os
from typing import List

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_BASE_URL = "https://api.commerce.naver.com/external"
_RECOMMENDED_MIN_PX = 1000  # 네이버쇼핑 이미지 권장 최소 해상도 (변, px)


def check_min_resolution(image_url: str, min_px: int = _RECOMMENDED_MIN_PX):
    """
    대표이미지 해상도가 네이버 권장 최소치(1000px)에 못 미치는지 확인.
    (width, height) 튜플 반환, 확인 실패시 None — 실패해도 등록을 막지는 않고
    호출부에서 경고만 표시하는 용도.
    """
    if not _HAS_REQUESTS or not image_url:
        return None
    try:
        from PIL import Image
        from io import BytesIO
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        return img.size
    except Exception:
        return None


def upload_images(image_urls: List[str], access_token: str) -> List[str]:
    """
    외부 이미지 URL들을 다운로드해서 네이버 이미지 서버에 업로드 → 네이버 URL 리스트 반환.
    실패한 개별 이미지는 건너뛰고 성공한 것만 반환.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    files = []
    for i, url in enumerate(image_urls[:10]):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            ext = "jpg"
            if "." in url.split("?")[0].rsplit("/", 1)[-1]:
                ext = url.split("?")[0].rsplit(".", 1)[-1][:4]
            files.append(("imageFiles", (f"image_{i}.{ext}", resp.content, "image/jpeg")))
        except Exception as e:
            print(f"  [경고] 이미지 다운로드 실패 ({url[:60]}...): {e}")

    if not files:
        return []

    resp = requests.post(
        f"{_BASE_URL}/v1/product-images/upload",
        headers={"Authorization": f"Bearer {access_token}"},
        files=files,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"이미지 업로드 실패 [{resp.status_code}]: {resp.text[:300]}")

    result = resp.json()
    images = result.get("images", result if isinstance(result, list) else [])
    return [img.get("url", "") for img in images if img.get("url")]
