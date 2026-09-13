# -*- coding: utf-8 -*-
"""컷을 눈으로 읽어 쓸 것과 버릴 것을 가른다 (2026-09).

왜 필요한가 — 도매 상세설명의 텍스트 필드는 사실상 비어 있다(실측: 주걱·우산 모두
글자 0자, img 태그뿐). 상품 정보가 전부 이미지 안에 그림으로 들어 있어서, 글자를 읽지
않으면 "포인트 3줄"을 뽑을 근거 자체가 없다. 실제로 지금 등록되는 상세페이지는 포인트
블록이 통째로 비어 있다.

동시에 컷 중에는 소비자에게 나가면 안 되는 것이 섞여 있다 — 공급사 브랜드, 시험성적서,
사용설명서, "LOGO 인쇄주문가능" 같은 판촉물 단체주문 안내(실측: 우산 26컷 중 6컷).
지금은 이게 그대로 구매자 화면에 나가고 있다.

판독은 이 Mac의 Claude Code(지금 쓰는 구독)로 한다 — API 키·별도 결제 없음(claude_cli.py).
판독이 실패하거나 Claude를 못 쓰면 비율·위치만으로 결정적으로 분류한다 — 페이지는 어떤
경우에도 만들어져야 한다.

⚠ 읽어낸 수치·인증 문구는 확신이 낮으면 버린다. 잘못 읽은 숫자가 상세페이지에 나가면
그건 허위표시다 — 문구 한 줄이 줄어드는 쪽이 언제나 싸다.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .claude_cli import ClaudeUnavailable, ask, unavailable_reason
from .cuts import CUTS_DIR, Cut, CutSet, load_cuts

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_MODEL = "claude-sonnet-5"
# 판독 정확도가 곧 허위표시 위험이라 여기만 Sonnet을 쓴다 — "LOGO 인쇄주문가능"이
# 소비자용이 아니라는 판단에는 맥락 이해가 필요하고, 이미지 속 한글을 잘못 읽으면
# 그대로 상세페이지에 나간다. 문구 작성(layout.py)은 Haiku로 충분하다.
_READ_TIMEOUT = 420
_READ_THINK = False   # 글자 거르기를 글자 인식이 맡으므로 판독은 생각 없이 — 시간·사용량이 크게 준다

# Mac 내장 글자 인식 — 설치·결제·구독 한도 없음. 처음 쓸 때 한 번 컴파일한다.
_OCR_SRC = Path(__file__).with_name("cut_ocr.swift")
_OCR_BIN = CUTS_DIR.parent / "bin" / "cut_ocr"

# 모아찍기 — 컷을 한 장씩 보내면 사진마다 한 번씩 왕복해 29컷에 104초가 걸렸다(실측).
# 번호표를 붙여 1~2장으로 모아 보낸다.
_SHEET_COL_W = 320       # 컷 한 장의 폭 — 글자를 읽을 수 있는 최소선
_SHEET_GAP = 10
_SHEET_LABEL_H = 30      # 번호표 띠 — 컷 위에 따로 둬서 사진 속 글자를 가리지 않는다
_SHEET_ONE_MAX = 3500    # 모은 높이가 이보다 크면 두 장으로 나눈다
_SHEET_MAX_EDGE = 1568   # 이보다 큰 이미지는 모델 쪽에서 줄여 작은 글자가 뭉개진다

# 좋은 사진 속에 섞여 있던 공급사·도매 정보(실측: 우산 5번 컷 아래 "우산케이스
# 5장단위판매3000원(개당600원)"). 판독이 "상품 사진"이라고 해도 이 글자가 읽히면 뺀다.
_B2B = re.compile(r"단위\s*판매|개당|도매|단체\s*주문|최소\s*주문|로고\s*인쇄|logo\s*인쇄|인쇄\s*주문"
                  r"|시험\s*성적서|성적서|면책|시험\s*장소|사업자\s*등록|통신\s*판매|고객\s*센터"
                  r"|customer\s*center|\d[\d,]*\s*원(?![가-힣])", re.I)
# 사실(facts)에서는 가격·거래조건만 거른다 — "시험성적서 기준 99.9%"는 상품의 사실이다.
_B2B_FACT = re.compile(r"단위\s*판매|개당|도매|단체\s*주문|최소\s*주문|\d[\d,]*\s*원(?![가-힣])")

KINDS = ("product", "explain", "spec", "supplier")


@dataclass
class CutRead:
    index: int
    kind: str = "product"     # product 상품사진 | explain 설명컷 | spec 도표 | supplier 공급사·B2B
    use: bool = True          # 소비자 화면에 내보내도 되는가
    text: str = ""            # 컷 안에 적힌 글자 (읽은 그대로)
    label: str = ""           # 색상명 등 짧은 라벨
    reason: str = ""          # 버리는 경우 그 이유
    recut: bool = False       # 좋은 사진에 금지 정보가 섞여 뺀 컷 — 범위를 고치면 살릴 수 있다


@dataclass
class Reading:
    goods_no: str
    reads: List[CutRead] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)   # 문구 생성의 근거가 되는 사실
    by_ai: bool = False
    read_at: str = ""
    fallback_reason: str = ""   # 규칙으로 떨어진 이유 — 화면에 보여준다(저장하지 않음)

    def of(self, index: int) -> CutRead:
        for r in self.reads:
            if r.index == index:
                return r
        return CutRead(index=index)

    def usable(self, kind: Optional[str] = None) -> List[int]:
        return [r.index for r in self.reads
                if r.use and (kind is None or r.kind == kind)]


# ── 글자 인식 ───────────────────────────────────────────────────────────
def ocr_cuts(cs: CutSet, cuts: List[Cut]) -> Dict[int, str]:
    """컷을 원본 크기에서 읽어 {컷번호: 글자}. 실패하면 빈 dict — 판독은 계속한다.

    모아찍기는 사진을 40%로 줄여 보내서 작은 글자를 놓친다(실측: 우산 5번 컷 아래
    "5장단위판매3000원(개당600원)"을 못 읽고 통과시킴). 가격·공급사 문구 거르기는 이
    결과로 한다. 우산 29컷 6초.
    """
    if not cuts:
        return {}
    try:
        if not _OCR_BIN.exists() or _OCR_BIN.stat().st_mtime < _OCR_SRC.stat().st_mtime:
            _OCR_BIN.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["swiftc", "-O", str(_OCR_SRC), "-o", str(_OCR_BIN)],
                           check=True, capture_output=True, timeout=300)
        paths = {str(cs.path_of(c)): c.index for c in cuts}
        out = subprocess.run([str(_OCR_BIN), *paths], capture_output=True, text=True,
                             timeout=300, check=True)
        return {paths[p]: t for p, t in json.loads(out.stdout).items() if p in paths}
    except Exception as e:
        print(f"  [경고] 글자 인식 실패 — 판독만으로 거릅니다 ({e})")
        return {}


# ── 폴백 — Claude 없이 비율·글자만으로 ──────────────────────────────────
def _fallback(cs: CutSet, ocr: Optional[Dict[int, str]] = None) -> Reading:
    """Claude를 못 쓰거나 판독이 실패했을 때. 지어내지 않고, 쓰기 애매한 것만 뺀다.

    비율이 극단적인 컷은 여러 컷이 안 나뉜 덩어리이거나 얇은 띠라 그대로 쓰면 페이지가
    망가진다. 글자 인식 결과가 있으면 가격·공급사 문구가 보이는 컷도 뺀다.
    """
    ocr = ocr or {}
    reads = []
    for c in cs.cuts:
        r = c.ratio
        if _B2B.search(ocr.get(c.index, "")):
            reads.append(CutRead(index=c.index, kind="product", use=False,
                                 text=ocr[c.index][:300], reason="가격·공급사 정보 글자가 보임"))
        elif r > 2.2:
            reads.append(CutRead(index=c.index, kind="product", use=False,
                                 reason="여러 컷이 안 나뉜 덩어리로 보임"))
        elif r < 0.28:
            reads.append(CutRead(index=c.index, kind="product", use=False,
                                 reason="너무 얇아 내용을 담기 어려움"))
        else:
            reads.append(CutRead(index=c.index, kind="product", use=True))
    return Reading(goods_no=cs.goods_no, reads=reads, by_ai=False,
                   read_at=time.strftime("%Y-%m-%dT%H:%M"))


# ── Claude 판독 ─────────────────────────────────────────────────────────
def build_sheets(cs: CutSet, cuts: List[Cut]) -> List[Path]:
    """컷을 번호표와 함께 1~2장으로 모은다.

    긴 컷도 자르지 않고 통째로 싣는다 — 긴 컷의 아래쪽에 도매 가격이 숨어 있던 일이
    있다(우산 5번 컷). 한 장이 _SHEET_MAX_EDGE를 넘지 않게 열을 나눠 정사각형에 가깝게 쌓는다.
    """
    items = []
    for c in cuts:
        try:
            im = Image.open(cs.path_of(c)).convert("RGB")
        except Exception:
            continue
        h = max(1, round(im.height * _SHEET_COL_W / im.width))
        items.append((c.index, im.resize((_SHEET_COL_W, h), Image.LANCZOS)))
    if not items:
        return []

    def tall(it):
        return it[1].height + _SHEET_LABEL_H + _SHEET_GAP

    def split(seq, parts):
        """순서를 지키며 높이가 비슷하게 parts개로 나눈다."""
        target = sum(tall(it) for it in seq) / parts
        out, cur, acc = [], [], 0
        for it in seq:
            if cur and acc + tall(it) / 2 > target and len(out) < parts - 1:
                out.append(cur)
                cur, acc = [], 0
            cur.append(it)
            acc += tall(it)
        out.append(cur)
        return out

    total = sum(tall(it) for it in items)
    font = ImageFont.load_default(size=24)
    paths = []
    for gi, group in enumerate(split(items, 1 if total <= _SHEET_ONE_MAX else 2)):
        gh = sum(tall(it) for it in group)
        cols = max(1, min(len(group), round(math.sqrt(gh / (_SHEET_COL_W + _SHEET_GAP)))))
        columns = split(group, cols)
        W = len(columns) * (_SHEET_COL_W + _SHEET_GAP) + _SHEET_GAP
        H = max(sum(tall(it) for it in col) for col in columns) + _SHEET_GAP
        sheet = Image.new("RGB", (W, H), (70, 70, 70))
        draw = ImageDraw.Draw(sheet)
        for ci, col in enumerate(columns):
            x, y = _SHEET_GAP + ci * (_SHEET_COL_W + _SHEET_GAP), _SHEET_GAP
            for idx, im in col:
                draw.rectangle((x, y, x + _SHEET_COL_W - 1, y + _SHEET_LABEL_H - 1), fill=(210, 30, 30))
                draw.text((x + 8, y + 2), f"#{idx}", fill="white", font=font)
                sheet.paste(im, (x, y + _SHEET_LABEL_H))
                y += tall((idx, im))
        scale = min(1.0, _SHEET_MAX_EDGE / max(W, H))
        if scale < 1:
            sheet = sheet.resize((round(W * scale), round(H * scale)), Image.LANCZOS)
        out = CUTS_DIR / cs.goods_no / f"read_sheet_{gi}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out, "JPEG", quality=85)
        paths.append(out)
    return paths


_PROMPT = """네이버 스마트스토어에 올릴 상품 상세페이지를 만들려고 한다.
도매 공급사가 준 상세 이미지를 컷 단위로 쪼개, 아래 이미지 파일 {n}장에 모아 두었다.
각 컷 위의 빨간 띠에 적힌 #번호가 그 컷의 번호다. 파일을 전부 Read 도구로 읽고 컷마다 판정해라.

{files}

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
  ⚠ 컷의 일부(위·아래 가장자리 포함)에라도 가격, "OO단위 판매", 도매·단체주문 조건,
  시험성적서 문구·면책조항, 공급사 연락처·주소, 따로 파는 다른 상품이 보이면 false다.
  나머지가 좋은 상품 사진이어도 false로 해라.
- mixed: 위 이유로 false인데 컷 안에 쓸 만한 상품 사진도 함께 들어 있으면 true.
- text: 컷 안에 적힌 글자를 읽은 그대로. 가장자리 작은 글자까지. 없으면 빈 문자열.
- label: 색상 컷이면 그 색 이름만(예: "네이비"). 아니면 빈 문자열.

아래는 같은 컷들을 원본 크기에서 글자 인식으로 읽은 결과다(오탈자가 있을 수 있다).
모아 둔 이미지에서 작은 글자가 잘 안 보여도 여기에 있으면 그 컷에 실제로 적힌 글자다.
{ocr}

마지막에 facts: 이 컷들에서 읽어낸 상품의 사실을 짧은 구로 모아라.
규칙 — 이미지에 실제로 적혀 있거나 사진으로 명백히 보이는 것만 넣어라. 추측하지 마라.
숫자·인증·효능은 또렷하게 읽히지 않으면 아예 빼라. 틀린 숫자가 나가면 허위광고가 된다.
가격·판매 단위·도매 조건은 facts에 넣지 마라.

JSON만 출력해라. 설명하지 마라.
{{"cuts":[{{"i":0,"kind":"product","use":true,"mixed":false,"text":"","label":""}}],"facts":["..."]}}"""


def _parse(raw: str, n: int, ocr: Optional[Dict[int, str]] = None) -> Optional[Reading]:
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
        seen_text = (ocr or {}).get(i, "")
        text = (str(c.get("text", "")) or seen_text)[:300]
        use = bool(c.get("use", True)) and kind != "supplier"
        # 판독의 판정을 그대로 믿지 않는다 — 좋은 사진에 섞인 도매 가격을 "상품 사진"으로
        # 보고 통과시킨 일이 있다(우산 5번 컷, 2026-09). 원본 크기 글자 인식 결과로도 본다.
        leak = bool(_B2B.search(text) or _B2B.search(seen_text))
        mixed = kind != "supplier" and (bool(c.get("mixed")) or leak)
        if leak or mixed:
            use = False
        reason = ("좋은 사진에 가격·공급사 정보가 섞여 있음" if mixed
                  else "공급사 자료" if kind == "supplier" else "")
        reads.append(CutRead(index=i, kind=kind, use=use, text=text,
                             label=str(c.get("label", ""))[:20], reason=reason, recut=mixed))
    if not reads:
        return None
    facts = [str(f)[:120] for f in d.get("facts", [])
             if str(f).strip() and not _B2B_FACT.search(str(f))][:12]
    return Reading(goods_no="", reads=reads, facts=facts, by_ai=True,
                   read_at=time.strftime("%Y-%m-%dT%H:%M"))


def read_cuts(cs: CutSet, product_name: str = "", category: str = "",
              force: bool = False) -> Reading:
    """컷을 판독한다. 저장된 판독이 있으면 그걸 쓰고, Claude를 못 쓰거나 실패하면 폴백."""
    if not force:
        cached = load_reading(cs.goods_no)
        if cached:
            return cached

    targets = [c for c in cs.cuts if c.source == "domemae"]
    # 글자 인식은 Claude를 못 쓸 때도 돈다 — 규칙 기반으로 떨어져도 가격·공급사 컷은 뺀다
    ocr = ocr_cuts(cs, targets)

    def fall(reason: str) -> Reading:
        print(f"  [경고] 컷 판독 — 비율·글자 기준으로 대체합니다 ({reason})")
        r = _fallback(cs, ocr)
        r.fallback_reason = reason
        save_reading(r)
        return r

    if not _HAS_PIL or not cs.cuts:
        return fall("판독할 사진이 없거나 Pillow가 없습니다")
    why = unavailable_reason()
    if why:
        return fall(why)

    sheets = build_sheets(cs, targets)
    if not sheets:
        return fall("판독할 사진 파일을 열 수 없습니다")
    ocr_lines = "\n".join(f"#{i}: {' / '.join(t.split(chr(10)))[:300]}"
                          for i, t in sorted(ocr.items()) if t.strip()) or "(글자 인식 결과 없음)"
    prompt = _PROMPT.format(n=len(sheets), files="\n".join(f"- {p}" for p in sheets), ocr=ocr_lines,
                            name=product_name or "(상품명 없음)", category=category or "(카테고리 없음)")
    try:
        parsed = _parse(ask(prompt, model=_MODEL, files=sheets, timeout=_READ_TIMEOUT, think=_READ_THINK),
                        max(c.index for c in cs.cuts) + 1, ocr)
    except ClaudeUnavailable as e:
        return fall(str(e))
    if parsed is None:
        return fall("판독 결과를 읽을 수 없습니다")

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

    # (f) 좋은 사진에 섞인 도매 가격 — 판독이 "상품 사진, 써도 됨"이라고 해도 뺀다(우산 5번 컷)
    r4 = _parse('{"cuts":[{"i":0,"kind":"product","use":true,"text":"블랙 네이비 우산케이스 5장단위판매3000원(개당600원)"},'
                '{"i":1,"kind":"supplier","use":false,"text":"시험성적서"},'
                '{"i":2,"kind":"explain","use":true,"text":"원터치 자동우산 99% 자외선 차단"}],'
                '"facts":["UV 차단율 99.9%(시험성적서 기준)","케이스 개당 600원"]}', 4)
    assert r4.of(0).use is False, "좋은 사진에 섞인 도매 가격이 소비자 화면으로 나감"
    assert r4.of(0).recut is True, "살릴 수 있는 컷에 범위 고치기 표시가 안 붙음"
    assert r4.of(1).recut is False, "공급사 자료 컷에까지 범위 고치기를 권함"
    assert r4.of(2).use is True, "'원터치'의 '원'을 가격으로 오인해 멀쩡한 컷을 뺌"
    assert r4.facts == ["UV 차단율 99.9%(시험성적서 기준)"], f"도매 가격이 문구 재료로 들어감: {r4.facts}"

    # (h) 판독이 작은 글자를 놓쳐도 원본 크기 글자 인식에 가격이 보이면 뺀다(우산 5번 컷 실측)
    r5 = _parse('{"cuts":[{"i":0,"kind":"explain","use":true,"text":"블랙 네이비 (색상안내)"}]}', 4,
                ocr={0: "시험장소 : 경기도 안양시\n우산케이스\n5장단위판매3000원(개당600원)"})
    assert r5.of(0).use is False and r5.of(0).recut is True, "글자 인식에 잡힌 도매 가격을 판독이 덮어씀"

    # (i) Claude를 못 써도 글자 인식으로 공급사 컷은 뺀다
    fb2 = _fallback(cs, {3: "고객센터 070-7518-8002"})
    assert fb2.usable() == [0], f"규칙 기반이 고객센터 컷을 통과시킴: {fb2.usable()}"

    # (g) 모아찍기 — 컷이 많아도 1~2장, 한 장이 모델 한계 크기를 넘지 않는다
    import shutil
    from PIL import Image as _Im
    gcs = CutSet(goods_no="_demo_sheet", cuts=[
        Cut(index=i, filename=f"{i:03d}.jpg", width=800, height=400 + (i * 137) % 1600) for i in range(40)])
    d = CUTS_DIR / gcs.goods_no
    d.mkdir(parents=True, exist_ok=True)
    try:
        for c in gcs.cuts:
            _Im.new("RGB", (c.width, c.height), (200, 200, 200)).save(gcs.path_of(c))
        sheets = build_sheets(gcs, gcs.cuts)
        assert 1 <= len(sheets) <= 2, f"모아찍기가 {len(sheets)}장 — 1~2장이어야 함"
        for sp in sheets:
            w, h = _Im.open(sp).size
            assert max(w, h) <= _SHEET_MAX_EDGE, f"모아찍기 {w}x{h} — 줄여지면 글자가 뭉개짐"
        few = build_sheets(gcs, gcs.cuts[:3])
        assert len(few) == 1, "컷이 적은데 두 장으로 나눔"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("cut_reader._demo self-check OK")


if __name__ == "__main__":
    _demo()
