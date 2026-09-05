"""
네이버 커머스 API 이미지 업로드.

상품 등록시 이미지 URL은 네이버 자체 이미지 서버(shop-phinf.pstatic.net 등)에
업로드된 URL만 허용됨 — 외부(도매매 등) CDN URL은 InvalidImageUrl 오류 발생
(2026-07-12 실전 테스트로 확인).

API: POST https://api.commerce.naver.com/external/v1/product-images/upload
Content-Type: multipart/form-data, 필드명 imageFiles (최대 10개)
"""
import os
from io import BytesIO
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

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
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        return img.size
    except Exception:
        return None


def pad_to_square(image_bytes: bytes, background=(255, 255, 255)) -> bytes:
    """
    대표이미지를 1:1 정사각으로 맞춘다 — 크롭(잘라내기)이 아니라 흰 배경 패딩이다.
    네이버쇼핑 목록 썸네일은 정사각으로 강제 표시되는데, 원본이 직사각형이면 좌우나
    위아래가 잘려 상품 일부(가격표·구성품 등)가 안 보이는 경우가 있었다(2026-09).
    잘라내면 정보가 사라지므로, 짧은 변에 흰 여백을 더해 정사각으로 맞춘다.
    이미 정사각이면 그대로 반환(불필요한 재인코딩 방지).
    """
    if not _HAS_PIL:
        return image_bytes
    try:
        img = Image.open(BytesIO(image_bytes))
        img = img.convert("RGB") if img.mode != "RGB" else img
        w, h = img.size
        if w == h:
            return image_bytes
        side = max(w, h)
        canvas = Image.new("RGB", (side, side), background)
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return image_bytes  # 가공 실패해도 원본 그대로 업로드 — 대표이미지가 아예 빠지는 것보단 낫다


def upload_images(image_urls: List[str], access_token: str, square_first: bool = True) -> List[Optional[str]]:
    """
    외부 이미지 URL들을 다운로드해서 네이버 이미지 서버에 업로드 → 네이버 URL 리스트 반환.

    반환 리스트는 image_urls와 길이·순서가 항상 같다 — 실패한 개별 이미지는 그
    자리에 None을 채운다. 예전엔 실패분을 건너뛴 짧은 리스트를 반환해서, 호출부가
    dict(zip(originals, uploaded))로 원본-업로드 URL을 짝지을 때 실패 지점 이후
    전부 밀리는 사고가 있었다(5장 중 2장 실패 시 대표이미지까지 바뀜, 2026-09).

    square_first: 목록의 첫 번째(대표이미지)만 1:1 정사각으로 패딩한다 — 호출부
    (pipeline.py)가 항상 대표이미지를 index 0에 놓고 넘기는 관례를 따른다.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    image_urls = image_urls[:10]
    files = []
    positions = []  # files의 각 항목이 image_urls의 몇 번째였는지
    for i, url in enumerate(image_urls):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            ext = "jpg"
            if "." in url.split("?")[0].rsplit("/", 1)[-1]:
                ext = url.split("?")[0].rsplit(".", 1)[-1][:4]
            image_bytes = resp.content
            if i == 0 and square_first:
                image_bytes = pad_to_square(image_bytes)
                ext = "jpg"  # pad_to_square는 항상 JPEG로 재인코딩
            files.append(("imageFiles", (f"image_{i}.{ext}", image_bytes, "image/jpeg")))
            positions.append(i)
        except Exception as e:
            print(f"  [경고] 이미지 다운로드 실패 ({url[:60]}...): {e}")

    results: List[Optional[str]] = [None] * len(image_urls)
    if not files:
        return results

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
    uploaded_urls = [img.get("url", "") for img in images if img.get("url")]
    # 네이버 응답이 요청한 files 순서와 같다고 가정(문서에 순서 보장 명시 없음 — 기존
    # 동작 유지). 서버가 일부를 거부해 응답 개수가 files보다 적으면 뒤쪽은 None으로 남는다.
    for pos, url in zip(positions, uploaded_urls):
        results[pos] = url
    return results


def _demo() -> None:
    """실행 가능한 자체 점검 — pad_to_square가 직사각형은 정사각으로 맞추고
    정사각은 그대로 두는지 확인 (네트워크 호출 없음)."""
    if not _HAS_PIL:
        print("Pillow 미설치 — pad_to_square 자체 점검 건너뜀")
        return

    def _make(w, h):
        buf = BytesIO()
        Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="JPEG")
        return buf.getvalue()

    wide = pad_to_square(_make(800, 400))
    img = Image.open(BytesIO(wide))
    assert img.size == (800, 800), img.size

    tall = pad_to_square(_make(400, 800))
    img2 = Image.open(BytesIO(tall))
    assert img2.size == (800, 800), img2.size

    square_bytes = _make(500, 500)
    assert pad_to_square(square_bytes) == square_bytes, "이미 정사각인데 재인코딩됨"

    print("images.pad_to_square self-check OK")

    _demo_upload_partial_failure()


def _demo_upload_partial_failure() -> None:
    """원본 5장 중 2장(인덱스 1, 3) 다운로드가 실패해도 반환 리스트가 원본과 같은
    길이·순서를 유지하는지 확인 (requests를 흉내만 내고 실제 네트워크는 안 씀).
    예전엔 실패분을 건너뛴 짧은 리스트를 반환해서 zip()이 밀렸다(2026-09)."""
    from unittest.mock import patch, Mock

    urls = [f"https://dome.example/img{i}.jpg" for i in range(5)]

    def fake_get(url, timeout=10):
        if url in (urls[1], urls[3]):
            raise Exception("다운로드 실패(시뮬)")
        resp = Mock(content=b"fake-image-bytes")
        resp.raise_for_status = lambda: None
        return resp

    def fake_post(*args, **kwargs):
        # 다운로드 성공한 3장(0, 2, 4)만 업로드 요청에 실렸을 것 — 그 3개에 대해서만 URL 반환
        resp = Mock(status_code=200)
        resp.json = lambda: {"images": [{"url": f"https://naver.example/n{i}.jpg"} for i in range(3)]}
        return resp

    with patch("bebrave.smartstore.images.requests.get", side_effect=fake_get), \
         patch("bebrave.smartstore.images.requests.post", side_effect=fake_post):
        result = upload_images(urls, access_token="x")

    assert len(result) == len(urls), "반환 길이가 원본과 달라짐 — 매핑이 밀릴 수 있음"
    assert result[1] is None and result[3] is None, "실패한 자리에 None이 안 채워짐"
    assert result[0] and result[2] and result[4], "성공한 이미지가 누락됨"
    print("images.upload_images 부분 실패 시뮬 self-check OK")


if __name__ == "__main__":
    _demo()
