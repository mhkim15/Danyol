# -*- coding: utf-8 -*-
"""컷을 눈으로 읽어 쓸 것과 버릴 것을 가른다 (2026-09).

왜 필요한가 — 도매 상세설명의 텍스트 필드는 사실상 비어 있다(실측: 주걱·우산 모두
글자 0자, img 태그뿐). 상품 정보가 전부 이미지 안에 그림으로 들어 있어서, 글자를 읽지
않으면 "포인트 3줄"을 뽑을 근거 자체가 없다. 실제로 지금 등록되는 상세페이지는 포인트
블록이 통째로 비어 있다.

동시에 컷 중에는 소비자에게 나가면 안 되는 것이 섞여 있다 — 공급사 브랜드, 시험성적서,
사용설명서, "LOGO 인쇄주문가능" 같은 판촉물 단체주문 안내(실측: 우산 26컷 중 6컷).
지금은 이게 그대로 구매자 화면에 나가고 있다.

판독이 실패하거나 키가 없으면 비율·위치만으로 결정적으로 분류한다 — 페이지는 어떤
경우에도 만들어져야 한다.

⚠ 읽어낸 수치·인증 문구는 확신이 낮으면 버린다. 잘못 읽은 숫자가 상세페이지에 나가면
그건 허위표시다 — 문구 한 줄이 줄어드는 쪽이 언제나 싸다.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .cuts import CUTS_DIR, Cut, CutSet, load_cuts

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_MODEL = "claude-sonnet-5"
# 판독 정확도가 곧 허위표시 위험이라 여기만 Sonnet을 쓴다 — "LOGO 인쇄주문가능"이
# 소비자용이 아니라는 판단에는 맥락 이해가 필요하고, 이미지 속 한글을 잘못 읽으면
# 그대로 상세페이지에 나간다. 문구 작성(content.py)은 Haiku로 충분하다.

_READ_WIDTH = 320        # 판독용 축소 폭 — 글자를 읽을 수 있는 최소선
_READ_MAX_HEIGHT = 900   # 세로로 긴 컷이 토큰을 독차지하지 않게
_MAX_CUTS_PER_CALL = 30

KINDS = ("product", "explain", "spec", "supplier")


@dataclass
class CutRead:
    index: int
    kind: str = "product"     # product 상품사진 | explain 설명컷 | spec 도표 | supplier 공급사·B2B
    use: bool = True          # 소비자 화면에 내보내도 되는가
    text: str = ""            # 컷 안에 적힌 글자 (읽은 그대로)
    label: str = ""           # 색상명 등 짧은 라벨
    reason: str = ""          # 버리는 경우 그 이유


@dataclass
class Reading:
    goods_no: str
    reads: List[CutRead] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)   # 문구 생성의 근거가 되는 사실
    by_ai: bool = False
    read_at: str = ""

    def of(self, index: int) -> CutRead:
        for r in self.reads:
            if r.index == index:
                return r
        return CutRead(index=index)

    def usable(self, kind: Optional[str] = None) -> List[int]:
        return [r.index for r in self.reads
                if r.use and (kind is None or r.kind == kind)]


# ── 폴백 — AI 없이 비율만으로 ────────────────────────────────────────────
def _fallback(cs: CutSet) -> Reading:
    """키가 없거나 판독이 실패했을 때. 지어내지 않고, 쓰기 애매한 것만 뺀다.

    비율이 극단적인 컷은 여러 컷이 안 나뉜 덩어리이거나 얇은 띠라 그대로 쓰면 페이지가
    망가진다. 공급사 자료는 여기서 걸러낼 방법이 없으므로 화면에 그 사실을 알린다.
    """
    reads = []
    for c in cs.cuts:
        r = c.ratio
        if r > 2.2:
            reads.append(CutRead(index=c.index, kind="product", use=False,
                                 reason="여러 컷이 안 나뉜 덩어리로 보임"))
        elif r < 0.28:
            reads.append(CutRead(index=c.index, kind="product", use=False,
                                 reason="너무 얇아 내용을 담기 어려움"))
        else:
            reads.append(CutRead(index=c.index, kind="product", use=True))
    return Reading(goods_no=cs.goods_no, reads=reads, by_ai=False,
                   read_at=time.strftime("%Y-%m-%dT%H:%M"))


# ── AI 판독 ─────────────────────────────────────────────────────────────
def _encode(path: Path) -> Optional[dict]:
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    h = int(im.height * _READ_WIDTH / im.width)
    im = im.resize((_READ_WIDTH, max(1, h)), Image.LANCZOS)
    if im.height > _READ_MAX_HEIGHT:     # 긴 컷은 위쪽만 — 제목·설명은 대개 위에 있다
        im = im.crop((0, 0, im.width, _READ_MAX_HEIGHT))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


_PROMPT = """네이버 스마트스토어에 올릴 상품 상세페이지를 만들려고 한다.
아래는 도매 공급사가 준 상세 이미지를 컷 단위로 쪼갠 것이다. 컷마다 판정해라.

상품: {name}
카테고리: {category}

각 컷을 다음 넷 중 하나로 분류한다.
- product: 상품만 찍힌 사진. 글자가 없거나 아주 적다. (페이지 본문에 쓸 소재)
- explain: 상품 사진 위에 설명 문구나 아이콘이 그려진 컷.
- spec: 치수·무게·구성 같은 수치를 담은 도표.
- supplier: 소비자에게 보이면 안 되는 것. 공급사 브랜드·로고, 시험성적서, 사용설명서,
  도매 주문 안내, "LOGO 인쇄 주문가능" 같은 단체주문·판촉물 안내, 다른 쇼핑몰 흔적.

그리고 컷마다:
- use: 소비자 상세페이지에 내보내도 되면 true. supplier는 무조건 false.
  상품과 무관한 컷, 글자만 빽빽한 컷도 false.
- text: 컷 안에 적힌 글자를 읽은 그대로. 없으면 빈 문자열.
- label: 색상 컷이면 그 색 이름만(예: "네이비"). 아니면 빈 문자열.

마지막에 facts: 이 컷들에서 읽어낸 상품의 사실을 짧은 구로 모아라.
규칙 — 이미지에 실제로 적혀 있거나 사진으로 명백히 보이는 것만 넣어라. 추측하지 마라.
숫자·인증·효능은 또렷하게 읽히지 않으면 아예 빼라. 틀린 숫자가 나가면 허위광고가 된다.

JSON만 출력해라. 설명하지 마라.
{{"cuts":[{{"i":0,"kind":"product","use":true,"text":"","label":""}}],"facts":["..."]}}"""


def _parse(raw: str, n: int) -> Optional[Reading]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    reads = []
    for c in d.get("cuts", []):
        try:
            i = int(c["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= i < n:
            continue
        kind = c.get("kind", "product")
        if kind not in KINDS:
            kind = "product"
        reads.append(CutRead(index=i, kind=kind,
                             use=bool(c.get("use", True)) and kind != "supplier",
                             text=str(c.get("text", ""))[:300],
                             label=str(c.get("label", ""))[:20]))
    if not reads:
        return None
    facts = [str(f)[:120] for f in d.get("facts", []) if str(f).strip()][:12]
    return Reading(goods_no="", reads=reads, facts=facts, by_ai=True,
                   read_at=time.strftime("%Y-%m-%dT%H:%M"))


def read_cuts(cs: CutSet, product_name: str = "", category: str = "",
              force: bool = False) -> Reading:
    """컷을 판독한다. 캐시가 있으면 그걸 쓰고, 키가 없거나 실패하면 폴백."""
    if not force:
        cached = load_reading(cs.goods_no)
        if cached:
            return cached

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not _HAS_PIL or not cs.cuts:
        r = _fallback(cs)
        save_reading(r)
        return r

    try:
        import anthropic
    except ImportError:
        r = _fallback(cs)
        save_reading(r)
        return r

    targets = [c for c in cs.cuts if c.source == "domemae"][:_MAX_CUTS_PER_CALL]
    blocks = []
    for c in targets:
        enc = _encode(cs.path_of(c))
        if enc is None:
            continue
        blocks.append({"type": "text", "text": f"[컷 {c.index}]"})
        blocks.append(enc)
    if not blocks:
        r = _fallback(cs)
        save_reading(r)
        return r

    blocks.append({"type": "text",
                   "text": _PROMPT.format(name=product_name or "(상품명 없음)",
                                          category=category or "(카테고리 없음)")})
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(model=_MODEL, max_tokens=4000,
                                     messages=[{"role": "user", "content": blocks}])
        parsed = _parse(msg.content[0].text, max(c.index for c in cs.cuts) + 1)
    except Exception as e:
        print(f"  [경고] 컷 판독 실패 — 비율 기준으로 대체합니다 ({e})")
        parsed = None

    if parsed is None:
        r = _fallback(cs)
        save_reading(r)
        return r

    parsed.goods_no = cs.goods_no
    # 판독이 빠뜨린 컷은 버리지 않고 기본값으로 채운다 — 응답이 짧게 잘려도 페이지가 빈다.
    seen = {r.index for r in parsed.reads}
    for c in cs.cuts:
        if c.index not in seen:
            parsed.reads.append(CutRead(index=c.index, use=(c.source == "seller")))
    parsed.reads.sort(key=lambda r: r.index)
    save_reading(parsed)
    return parsed


# ── 캐시 ────────────────────────────────────────────────────────────────
def _reading_path(goods_no: str) -> Path:
    return CUTS_DIR / str(goods_no) / "reading.json"


def load_reading(goods_no: str) -> Optional[Reading]:
    p = _reading_path(goods_no)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return Reading(goods_no=d["goods_no"], facts=d.get("facts", []),
                       by_ai=d.get("by_ai", False), read_at=d.get("read_at", ""),
                       reads=[CutRead(**r) for r in d.get("reads", [])])
    except Exception:
        return None


def save_reading(r: Reading) -> None:
    if not r.goods_no:
        return
    p = _reading_path(r.goods_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"goods_no": r.goods_no, "facts": r.facts, "by_ai": r.by_ai,
                             "read_at": r.read_at, "reads": [asdict(x) for x in r.reads]},
                            ensure_ascii=False), encoding="utf-8")


def _demo() -> None:
    """자체 점검 — 네트워크·API 호출 없음."""
    cs = CutSet(goods_no="_demo", cuts=[
        Cut(index=0, filename="000.jpg", width=800, height=800),    # 정상
        Cut(index=1, filename="001.jpg", width=800, height=3500),   # 덩어리
        Cut(index=2, filename="002.jpg", width=800, height=160),    # 얇은 띠
        Cut(index=3, filename="003.jpg", width=800, height=520),    # 정상
    ])

    # (a) 키가 없어도 쓸 컷이 남아야 한다 — 페이지는 어떤 경우에도 만들어져야 함
    fb = _fallback(cs)
    assert fb.usable() == [0, 3], f"폴백이 고른 컷이 이상함: {fb.usable()}"
    assert not fb.by_ai

    # (b) 공급사 자료는 판독이 use=true로 줘도 내보내지 않는다
    r = _parse('{"cuts":[{"i":0,"kind":"supplier","use":true,"text":"LOGO 인쇄주문가능"},'
               '{"i":1,"kind":"product","use":true}],"facts":["자동 개폐"]}', 4)
    assert r is not None
    assert r.of(0).use is False, "공급사 자료가 소비자 화면으로 나감"
    assert r.of(1).use is True
    assert r.facts == ["자동 개폐"]

    # (c) 응답 범위 밖의 컷 번호는 버린다 (엉뚱한 컷이 배치되는 것 방지)
    r2 = _parse('{"cuts":[{"i":99,"kind":"product","use":true},{"i":0,"kind":"product","use":true}]}', 4)
    assert [x.index for x in r2.reads] == [0], "존재하지 않는 컷 번호가 통과함"

    # (d) 깨진 응답은 None — 호출부가 폴백으로 떨어진다
    assert _parse("판독을 못 하겠습니다", 4) is None
    assert _parse('{"cuts":[]}', 4) is None

    # (e) 모르는 분류는 product로 강등하되 내보내기는 막지 않는다
    r3 = _parse('{"cuts":[{"i":0,"kind":"뭔가이상한값","use":true}]}', 4)
    assert r3.of(0).kind == "product"

    print("cut_reader._demo self-check OK")


if __name__ == "__main__":
    _demo()
