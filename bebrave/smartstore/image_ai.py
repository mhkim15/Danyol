"""
AI 이미지 생성 — 리메이크(트랙B) 상품 대표이미지를 도매매 원본 사진 "참고"만 해서
새로 만든다(2026-09, 사용자 요청). 상세설명 재생성(content.remake_detail_html)과 별개로,
이미지 자체를 새로 그리는 건 텍스트 생성과 다른 종류의 작업이라 별도 모듈로 둔다.

조사 결과(2026-09) — 참고 이미지를 넣어 새 이미지를 생성하는 기능이 실제 API로 열려있는
곳은 Google Gemini(별명 "나노바나나", 모델 gemini-2.5-flash-image 계열)와 OpenAI
gpt-image 계열이다. Midjourney는 디스코드 전용이라 공식 API가 없다. 이 중 Gemini를
골랐다 — 이 프로젝트가 이미 ANTHROPIC_API_KEY로 "키 있으면 쓰고 없으면 결정적 폴백"
패턴을 쓰고 있어(content.py 참고) 같은 구조를 그대로 재사용할 수 있고, 참고 이미지 입력 +
이미지 생성을 한 호출로 처리하는 API가 문서화돼 있다.

⚠ GEMINI_API_KEY가 이 프로젝트 .env에 아직 없어 이 함수는 실제 호출로 검증되지 않았다.
키를 넣고 처음 쓸 때는 반드시 결과 이미지를 눈으로 확인할 것.
"""
import base64
import os

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_MODEL = "gemini-2.5-flash-image"
_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"


def has_api_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", ""))


def generate_product_image(reference_image_url: str, prompt: str, api_key: str = "") -> bytes:
    """
    참고 이미지 1장 + 프롬프트 → 새로 생성된 이미지 바이트.

    reference_image_url: 도매매 원본 이미지 URL (구도·색감만 참고하고 그대로 베끼지
                          않도록 프롬프트에서 지시할 것 — 호출부 책임).
    prompt             : 생성 지시문. "이 사진을 참고해서 ~" 식으로 원본을 명시하는
                          프롬프트를 호출부에서 구성한다.

    실패(키 없음/네트워크/API 오류)하면 예외를 던진다 — 호출부가 "페이지로 이동" 등
    대체 수단을 안내해야 하므로 조용히 빈 값을 반환하지 않는다.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise ValueError(".env 파일에 GEMINI_API_KEY를 설정하세요.")

    ref_resp = requests.get(reference_image_url, timeout=15)
    ref_resp.raise_for_status()
    ref_b64 = base64.b64encode(ref_resp.content).decode("ascii")
    ref_mime = ref_resp.headers.get("Content-Type", "image/jpeg").split(";")[0] or "image/jpeg"

    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": ref_mime, "data": ref_b64}},
            ],
        }],
    }
    resp = requests.post(
        _API_URL,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"이미지 생성 실패 [{resp.status_code}]: {resp.text[:300]}")

    data = resp.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inline_data") or part.get("inlineData")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError("응답에 이미지 데이터가 없습니다 — API 응답 형식이 바뀌었을 수 있습니다.")


def build_remake_prompt(keyword: str, category: str) -> str:
    """지어내지 않는 원칙 그대로 — 실제 있는 정보(키워드·카테고리)만 프롬프트에 담는다."""
    return (
        f"참고 이미지 속 상품({keyword}, 카테고리: {category})의 실제 형태·색상·구성을 그대로 유지하면서, "
        "네이버쇼핑 대표이미지에 어울리는 깔끔한 흰색 배경의 정면 제품샷으로 다시 그려줘. "
        "글자나 워터마크, 실제 상품에 없는 장식은 넣지 마."
    )
