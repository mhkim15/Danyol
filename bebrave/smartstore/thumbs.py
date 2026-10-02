# -*- coding: utf-8 -*-
"""대표이미지(검색 목록 썸네일) 후보를 도매 사진에서 만들고, 하나를 추천한다 (2026-09).

왜 필요한가 — 대표이미지는 검색 목록에서 클릭을 가르는 한 장인데, 지금까지는 도매 원본
대표사진(또는 AI 버전 첫 화면 컷)이 검사 없이 그대로 나갔다. 네이버 대표이미지 기준
(2024-10-28~)은 "상품명과 맞는 상품만 깔끔하게"다 — 가격·배송·홍보 문구는 노출 제한·제재
대상이고, 흰 배경에 상품만 보이는 사진이 유리하다.

도매 상세 이미지에는 상품만 찍힌 칸이 설명 글자와 섞여 여러 개 들어 있다. 그래서
  1) 컷을 흰 여백 기준으로 칸 단위로 나누고 (Pillow만, 새 의존성 없음)
  2) Mac 내장 글자 인식으로 글자가 있는 칸을 빼고
  3) 흰 배경 정사각(1000px)으로 맞춘 뒤
  4) Claude(구독)가 번호표 붙인 모음 한 장을 보고 하나를 고른다. 못 쓰면 규칙 점수로 고른다.

새로 그리지 않는다 — 원본 픽셀을 자르고 여백만 붙인다. 생성형 AI는 형태·색을 바꿔 "실물과
다른 이미지"가 될 수 있다. Mac 내장 배경 제거도 실험했지만 바닥에 놓인 구성품을 지워버려
(돋보기 손톱깎이) 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .claude_cli import ClaudeUnavailable, ask
from .cuts import CUTS_DIR, build_cuts, load_cuts

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# 대표이미지는 매출에 직결되고 기준 위반은 제재라, 사진 판독과 같은 Sonnet을 쓴다
_MODEL = "claude-sonnet-5"

_WHITE = 236          # 이보다 밝으면 바탕으로 본다 (JPEG 잡티 감안)
_GAP = 4              # 바탕 줄이 이만큼 이어지면 칸 경계 — 엇갈려 놓인 칸 사이가 5px였다(손톱깎이)
_MIN_SIDE = 120       # 원본 기준 짧은 변 — 이보다 작으면 글자 줄·아이콘
_MIN_LONG = 250       # 원본 기준 긴 변
_MAX_DEPTH = 4
_MAX_REGIONS = 80     # 글자 인식에 넘길 칸 수 상한 — 긴 상세(우산 29컷)에서도 10초 안쪽
_MAX_TEXT = 8         # 공백 빼고 이만큼 글자가 읽히면 설명·홍보 칸으로 보고 뺀다
_MAX_THUMBS = 20      # 화면·Claude 모음에 올릴 후보 수
_OUT = 1000           # 네이버 권장 최소 변
_MARGIN = 0.85        # 흰 바탕에서 잘라낸 칸은 상품이 이만큼 차도록 여백을 둔다

Box = Tuple[int, int, int, int]

AUTO_EXTRAS = 5       # 자동으로 채우는 추가이미지 수 — 후보 뒤쪽은 비슷한 칸이 많다
MAX_EXTRAS = 9        # 네이버 추가이미지 상한


@dataclass
class Thumb:
    id: int
    filename: str         # 컷 폴더 안의 thumb_NN.jpg (1000px 정사각)
    cut: int              # 어느 컷에서 나왔나
    box: List[int]        # 컷 안의 위치 [x0, y0, x1, y1]
    px: int               # 잘라낸 원본의 긴 변 — 1000px로 키우기 전의 실제 해상도
    text: str = ""        # 글자 인식이 조금 읽은 것(기준 미만이라 통과)
    score: float = 0.0    # 규칙 점수 — Claude를 못 쓸 때 추천 기준


@dataclass
class ThumbSet:
    goods_no: str
    thumbs: List[Thumb] = field(default_factory=list)
    recommended: int = -1
    why: str = ""
    by_ai: bool = False
    fallback_reason: str = ""
    chosen: int = -1
    chosen_by: str = ""   # auto 추천이 자동 적용됨 | user 사람이 추천과 다른 걸 고름
    built_at: str = ""
    # 추가이미지(대표사진 아래 넘겨보는 사진) — 사람이 손대기 전엔 점수 상위 후보로 자동 채우고,
    # 한 번이라도 넣고 빼면 그 목록을 그대로 쓴다(2026-10). 예전엔 도매 상세 조각이 들어가
    # 작은 정사각 칸에서 잘린 모습으로 보였다.
    extras: List[int] = field(default_factory=list)
    extras_by: str = ""   # "" 자동 | user 사람이 고름

    def of(self, tid: int) -> Optional[Thumb]:
        return next((t for t in self.thumbs if t.id == tid), None)

    def extra_ids(self) -> List[int]:
        """등록에 쓸 추가이미지 후보 번호. 대표로 고른 사진은 뺀다."""
        if self.extras_by == "user":
            return [i for i in self.extras if self.of(i) and i != self.chosen][:MAX_EXTRAS]
        return [t.id for t in self.thumbs if t.id != self.chosen][:AUTO_EXTRAS]   # 점수순 정렬돼 있다

    def chosen_thumb(self) -> Optional[Thumb]:
        t = self.of(self.chosen)
        return t if t and (CUTS_DIR / self.goods_no / t.filename).exists() else None


# ── 저장 ────────────────────────────────────────────────────────────────
def _json_path(goods_no: str) -> Path:
    return CUTS_DIR / str(goods_no) / "thumbs.json"


def save_thumbs(ts: ThumbSet) -> None:
    p = _json_path(ts.goods_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(ts), ensure_ascii=False), encoding="utf-8")


def load_thumbs(goods_no: str) -> Optional[ThumbSet]:
    p = _json_path(goods_no)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["thumbs"] = [Thumb(**t) for t in d.get("thumbs", [])]
        return ThumbSet(**d)
    except Exception:
        return None


def chosen_thumb_path(goods_no: str) -> Optional[Path]:
    """등록에 쓸 대표이미지 파일. 화면에서 후보를 만든 적이 없으면 None — 예전 규칙대로 간다."""
    ts = load_thumbs(goods_no) if goods_no else None
    t = ts.chosen_thumb() if ts else None
    return CUTS_DIR / str(goods_no) / t.filename if t else None


def extra_thumb_paths(goods_no: str) -> Optional[List[Path]]:
    """등록에 쓸 추가이미지 파일들. 화면에서 후보를 만든 적이 없으면 None — 호출부가 예전 규칙으로
    채운다. 후보는 있는데 사람이 전부 뺐으면 빈 목록(추가이미지 없이 등록)."""
    ts = load_thumbs(goods_no) if goods_no else None
    if not ts:
        return None
    d = CUTS_DIR / str(goods_no)
    return [d / ts.of(i).filename for i in ts.extra_ids() if (d / ts.of(i).filename).exists()]


def toggle_extra(goods_no: str, tid: int) -> str:
    """추가이미지에 넣거나 뺀다. 실패하면 사유, 성공하면 빈 문자열."""
    ts = load_thumbs(goods_no)
    if not ts or not ts.of(tid):
        return "없는 후보입니다 — 새로고침하세요"
    if tid == ts.chosen:
        return "대표이미지로 고른 사진입니다 — 추가이미지에는 넣지 않습니다"
    cur = ts.extra_ids()
    if tid in cur:
        cur.remove(tid)
    elif len(cur) >= MAX_EXTRAS:
        return f"추가이미지는 {MAX_EXTRAS}장까지입니다"
    else:
        cur.append(tid)
    ts.extras, ts.extras_by = cur, "user"
    save_thumbs(ts)
    return ""


def reset_extras(goods_no: str) -> bool:
    ts = load_thumbs(goods_no)
    if not ts:
        return False
    ts.extras, ts.extras_by = [], ""
    save_thumbs(ts)
    return True


def choose(goods_no: str, tid: int) -> bool:
    ts = load_thumbs(goods_no)
    if not ts or not ts.of(tid):
        return False
    ts.chosen = tid
    ts.chosen_by = "auto" if tid == ts.recommended else "user"
    save_thumbs(ts)
    return True


# ── 칸 나누기 ───────────────────────────────────────────────────────────
def _content_mask(im: "Image.Image") -> "Image.Image":
    return im.convert("L").point(lambda v: 255 if v < _WHITE else 0)


def _profile(mask: "Image.Image", axis: int) -> List[int]:
    """axis 0: 가로줄마다, 1: 세로칸마다 내용 픽셀 비율(0~255)."""
    size = (1, mask.height) if axis == 0 else (mask.width, 1)
    return list(mask.resize(size, Image.BOX).getdata())


def _runs(prof: List[int], gap: int) -> List[Tuple[int, int]]:
    """바탕이 gap 이상 이어진 곳에서 끊은 내용 구간들. 칸 테두리 같은 가는 세로선은
    가로줄 비율로는 1% 미만이라 바탕으로 본다 — 그래서 테두리 안쪽 상품이 따로 떨어진다."""
    out, start, blank = [], None, 0
    for i, v in enumerate(prof):
        if v < 3:
            blank += 1
            if start is not None and blank >= gap:
                out.append((start, i - blank + 1))
                start = None
        else:
            if start is None:
                start = i
            blank = 0
    if start is not None:
        out.append((start, len(prof) - blank))
    return out


def _regions(mask: "Image.Image", box: Box, depth: int, out: List[Box]) -> None:
    """바탕 여백을 걷어낸 칸을 전부 모은다(부모 칸도 함께) — 여러 구성품이 떨어져 놓인
    사진은 자식으로 쪼개지지만 부모가 남아 있어야 세트 전체 사진을 잃지 않는다."""
    bb = mask.crop(box).getbbox()
    if not bb:
        return
    x0, y0 = box[0] + bb[0], box[1] + bb[1]
    x1, y1 = box[0] + bb[2], box[1] + bb[3]
    if x1 - x0 < _MIN_SIDE or y1 - y0 < _MIN_SIDE:
        return
    out.append((x0, y0, x1, y1))
    if depth >= _MAX_DEPTH:
        return
    sub = mask.crop((x0, y0, x1, y1))
    for axis in (0, 1):
        segs = _runs(_profile(sub, axis), _GAP)
        if len(segs) > 1:
            for a, b in segs:
                child = (x0, y0 + a, x1, y0 + b) if axis == 0 else (x0 + a, y0, x0 + b, y1)
                _regions(mask, child, depth + 1, out)
            return


def _area(b: Box) -> int:
    return (b[2] - b[0]) * (b[3] - b[1])


def _iou(a: Box, b: Box) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / float(_area(a) + _area(b) - inter) if inter else 0.0


def _ahash(im: "Image.Image") -> int:
    """거의 같은 사진 거르기용 — 도매 상세는 같은 사진을 여러 번 반복해 싣는다."""
    px = list(im.convert("L").resize((8, 8), Image.BOX).getdata())
    avg = sum(px) / 64
    return sum(1 << i for i, v in enumerate(px) if v > avg)


def _edge_mean(mask: "Image.Image") -> float:
    """잘라낸 칸 네 가장자리의 내용 비율 — 낮으면 흰 바탕 위 상품, 높으면 꽉 찬 사진."""
    rows, cols = _profile(mask, 0), _profile(mask, 1)
    return (rows[0] + rows[-1] + cols[0] + cols[-1]) / 4


def _square(crop: "Image.Image", white_bg: bool) -> "Image.Image":
    """흰 배경 정사각. 흰 바탕 칸엔 여백을 두고, 꽉 찬 사진은 짧은 변만 흰색으로 채운다."""
    w, h = crop.size
    side = round(max(w, h) / _MARGIN) if white_bg else max(w, h)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(crop, ((side - w) // 2, (side - h) // 2))
    return canvas.resize((_OUT, _OUT), Image.LANCZOS) if side != _OUT else canvas


def _score(px: int, edge: float, has_text: bool, main_photo: bool) -> float:
    return round(0.5 * (1 - edge / 255) + 0.3 * min(px, _OUT) / _OUT
                 + (0.0 if has_text else 0.2) + (0.05 if main_photo else 0.0), 3)


# ── 후보 만들기 ─────────────────────────────────────────────────────────
def build_thumbs(goods_no: str, image_urls: List[str]) -> ThumbSet:
    """도매 사진에서 대표이미지 후보를 만들어 저장한다. 추천·선택은 비워 둔다.

    잘라둔 컷이 있으면 그대로 쓴다 — 여기서 다시 자르면 사람이 만든 AI 버전 상세페이지가
    지워진다(build_cuts는 이미지 목록이 바뀌면 판독·구성을 버린다)."""
    if not _HAS_PIL:
        raise NotImplementedError("pip3 install Pillow 후 재시도하세요.")
    from .cut_reader import _B2B, load_reading, ocr_files

    goods_no = str(goods_no)
    cs = load_cuts(goods_no) or build_cuts(goods_no, image_urls)
    reading = load_reading(goods_no)
    d = cs.dir()
    for f in list(d.glob("thumb_*.jpg")) + [d / "thumb_sheet.jpg"]:
        f.unlink(missing_ok=True)

    regions, hashes = [], []    # (컷, 상자, 잘라낸 사진, 가장자리 비율, 대표사진 여부)
    for c in cs.cuts:
        if len(regions) >= _MAX_REGIONS:
            break
        if reading and c.source != "seller":
            r = reading.of(c.index)
            # 공급사 자료·치수 도표·무관한 컷은 뺀다(도표는 치수선이 그려져 있다 — 손톱깎이 "2.2cm").
            # 좋은 사진에 가격이 섞인 컷(recut)은 칸으로 나누면 살릴 수 있다
            if r.kind in ("supplier", "spec") or (not r.use and not r.recut):
                continue
        try:
            im = Image.open(cs.path_of(c)).convert("RGB")
        except Exception:
            continue
        mask = _content_mask(im)
        boxes: List[Box] = []
        _regions(mask, (0, 0, im.width, im.height), 0, boxes)
        kept: List[Box] = []
        for b in sorted(boxes, key=_area, reverse=True):
            w, h = b[2] - b[0], b[3] - b[1]
            if max(w, h) < _MIN_LONG or not (1 / 3 <= w / h <= 3):
                continue
            m = mask.crop(b)
            if m.resize((1, 1), Image.BOX).getpixel((0, 0)) < 20:
                continue            # 테두리만 있고 속이 빈 칸
            if any(_iou(b, k) > 0.85 for k in kept):
                continue
            crop = im.crop(b)
            hsh = _ahash(crop)
            if any(bin(hsh ^ o).count("1") <= 4 for o in hashes):
                continue
            kept.append(b)
            hashes.append(hsh)
            regions.append((c, b, crop, _edge_mean(m), c.source == "domemae" and c.src == 0))

    files = []
    for i, (_c, _b, crop, edge, _main) in enumerate(regions):
        f = d / f"thumb_{i:02d}.jpg"
        _square(crop, edge < 64).save(f, quality=90)
        files.append(f)
    texts = ocr_files(files)

    thumbs = []
    for i, ((c, b, crop, edge, main), f) in enumerate(zip(regions, files)):
        text = texts.get(str(f), "").strip()
        if len(re.sub(r"\s", "", text)) >= _MAX_TEXT or _B2B.search(text):
            f.unlink(missing_ok=True)
            continue
        px = max(crop.size)
        thumbs.append(Thumb(id=i, filename=f.name, cut=c.index, box=list(b), px=px,
                            text=text[:40], score=_score(px, edge, bool(text), main)))
    thumbs.sort(key=lambda t: -t.score)
    for t in thumbs[_MAX_THUMBS:]:
        (d / t.filename).unlink(missing_ok=True)

    ts = ThumbSet(goods_no=goods_no, thumbs=thumbs[:_MAX_THUMBS],
                  built_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    save_thumbs(ts)
    return ts


# ── 추천 ────────────────────────────────────────────────────────────────
_PROMPT = """네이버 스마트스토어 상품의 대표이미지(검색 목록에 뜨는 정사각 사진)를 고른다.
후보를 한 장에 모아 두었다. 이 파일을 Read 도구로 읽어라: {file}
각 후보 위 빨간 띠의 #번호가 후보 번호이고, 옆의 px는 원본 해상도다(작을수록 확대 시 흐리다).

상품명: {name}

네이버 대표이미지 기준(어기면 노출 제한·제재):
- 상품명과 일치하는 상품이 명확히 보여야 한다. 판매 구성과 다른 상품·색·수량이 섞이면 안 된다.
- 가격·할인·배송·홍보 문구가 들어가면 안 된다.
- 상품만 깔끔하게 보이는 사진이 유리하다(흰 배경, 손·소품·연출이 적을수록 좋음).

고르는 순서: 1) 기준 위반 없음 2) 무슨 상품인지 한눈에 알아보임 3) 선명함(px) 4) 깔끔함.
정확히 하나만 고르고 JSON만 출력: {{"pick": 번호, "why": "고른 이유 40자 이내"}}"""

_CELL, _LABEL_H, _SHEET_GAP, _COLS = 270, 28, 8, 4


def _sheet(ts: ThumbSet) -> Path:
    d = CUTS_DIR / ts.goods_no
    n = len(ts.thumbs)
    rows = -(-n // _COLS)
    W = _COLS * (_CELL + _SHEET_GAP) + _SHEET_GAP
    H = rows * (_CELL + _LABEL_H + _SHEET_GAP) + _SHEET_GAP
    sheet = Image.new("RGB", (W, H), (70, 70, 70))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for k, t in enumerate(ts.thumbs):
        x = _SHEET_GAP + (k % _COLS) * (_CELL + _SHEET_GAP)
        y = _SHEET_GAP + (k // _COLS) * (_CELL + _LABEL_H + _SHEET_GAP)
        draw.rectangle((x, y, x + _CELL - 1, y + _LABEL_H - 1), fill=(210, 30, 30))
        draw.text((x + 6, y + 3), f"#{t.id}  {t.px}px", fill="white", font=font)
        sheet.paste(Image.open(d / t.filename).convert("RGB").resize((_CELL, _CELL), Image.LANCZOS),
                    (x, y + _LABEL_H))
    out = d / "thumb_sheet.jpg"
    sheet.save(out, "JPEG", quality=85)
    return out


def recommend(goods_no: str, name: str) -> Optional[ThumbSet]:
    """후보 중 하나를 추천한다. 사람이 추천과 다른 걸 골라뒀으면 그 선택은 건드리지 않는다."""
    ts = load_thumbs(goods_no)
    if not ts or not ts.thumbs:
        return ts
    pick, why, reason = -1, "", ""
    try:
        sheet = _sheet(ts)
        raw = ask(_PROMPT.format(file=sheet, name=name), model=_MODEL, files=[sheet],
                  timeout=240, think=False)
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        if ts.of(int(d.get("pick", -1))):
            pick, why = int(d["pick"]), str(d.get("why", ""))[:60]
        else:
            reason = "Claude가 후보에 없는 번호를 답했습니다"
    except ClaudeUnavailable as e:
        reason = str(e)
    except Exception as e:
        reason = f"Claude 답을 읽지 못했습니다: {e}"[:200]

    ts.by_ai = pick >= 0
    if pick < 0:
        pick, why = ts.thumbs[0].id, "흰 배경·해상도·글자 없음 점수가 가장 높음"   # 점수순 정렬돼 있다
    ts.recommended, ts.why, ts.fallback_reason = pick, why, reason
    if ts.chosen_by != "user" or not ts.of(ts.chosen):
        ts.chosen, ts.chosen_by = pick, "auto"
    save_thumbs(ts)
    return ts


def _demo() -> None:
    """자체 점검 — 합성 이미지로만, 글자 인식·Claude 호출 없음."""
    if not _HAS_PIL:
        print("Pillow 없음 — 건너뜀")
        return

    # (a) 엇갈린 3단 구성(사진 칸 + 설명 글자) 컷에서 테두리 안쪽 상품만 칸으로 잡아내는가
    im = Image.new("RGB", (860, 1250), "white")
    dr = ImageDraw.Draw(im)
    dr.rectangle((5, 0, 430, 410), outline=(200, 200, 200))
    dr.rectangle((60, 40, 380, 380), fill=(230, 120, 90))
    dr.rectangle((432, 415, 855, 825), outline=(200, 200, 200))
    dr.rectangle((500, 480, 800, 760), fill=(120, 200, 120))
    for y in range(500, 700, 30):
        dr.rectangle((60, y, 400, y + 14), fill=(40, 40, 40))       # 왼쪽 설명 글자 줄
    dr.rectangle((10, 835, 428, 1238), outline=(200, 200, 200))
    dr.ellipse((80, 900, 360, 1180), fill=(90, 170, 230))
    for y in range(900, 1100, 30):
        dr.rectangle((450, y, 780, y + 14), fill=(40, 40, 40))      # 오른쪽 설명 글자 줄
    boxes: List[Box] = []
    _regions(_content_mask(im), (0, 0, 860, 1250), 0, boxes)
    near = lambda b, t: all(abs(p - q) <= 2 for p, q in zip(b, t))
    assert any(near(b, (80, 900, 361, 1181)) for b in boxes), f"테두리 안 상품을 못 잡음: {boxes}"
    assert any(near(b, (500, 480, 801, 761)) for b in boxes), f"오른쪽 칸 상품을 못 잡음: {boxes}"
    assert not any(b[3] - b[1] < _MIN_SIDE for b in boxes), "글자 한 줄 같은 얇은 조각이 후보 칸으로 남음"

    # (b) 흰 바탕 칸은 여백을 두고, 꽉 찬 사진은 채우기만 — 결과는 항상 1000px 정사각
    obj = Image.new("RGB", (400, 200), (90, 170, 230))
    sq = _square(obj, white_bg=True)
    assert sq.size == (_OUT, _OUT)
    assert sq.getpixel((_OUT // 2, 20)) == (255, 255, 255), "흰 바탕 칸인데 위쪽 여백이 없음"
    assert sq.getpixel((30, _OUT // 2)) == (255, 255, 255), "흰 바탕 칸인데 좌우 여백이 없음"
    full = _square(Image.new("RGB", (400, 400), (90, 170, 230)), white_bg=False)
    assert full.getpixel((3, 3)) != (255, 255, 255), "꽉 찬 정사각 사진에 여백을 붙임"

    # (c) 추천과 같은 걸 고르면 자동, 다른 걸 고르면 사람 선택 — 다시 추천해도 사람 선택은 유지
    import shutil
    g = "_demo_thumbs"
    shutil.rmtree(CUTS_DIR / g, ignore_errors=True)
    try:
        (CUTS_DIR / g).mkdir(parents=True)
        for i in range(2):
            sq.save(CUTS_DIR / g / f"thumb_{i:02d}.jpg")
        save_thumbs(ThumbSet(goods_no=g, recommended=0, chosen=0, chosen_by="auto",
                             thumbs=[Thumb(id=i, filename=f"thumb_{i:02d}.jpg", cut=0, box=[0, 0, 1, 1], px=500)
                                     for i in range(2)]))
        assert choose(g, 1) and load_thumbs(g).chosen_by == "user"
        assert chosen_thumb_path(g).name == "thumb_01.jpg", "등록에 고른 사진이 안 실림"
        assert choose(g, 0) and load_thumbs(g).chosen_by == "auto"
        assert not choose(g, 9), "없는 후보가 선택됨"

        # (d) 추가이미지 — 자동으로 대표 외 후보가 채워지고, 넣고 빼면 사람 선택으로 고정된다
        assert [p.name for p in extra_thumb_paths(g)] == ["thumb_01.jpg"], "자동 추가이미지가 안 채워짐"
        assert toggle_extra(g, 0), "대표로 고른 사진이 추가이미지에 들어감"
        assert toggle_extra(g, 1) == "" and extra_thumb_paths(g) == [], "뺐는데 남아 있음"
        assert load_thumbs(g).extras_by == "user"
        assert reset_extras(g) and len(extra_thumb_paths(g)) == 1, "자동으로 되돌리기 실패"
        assert extra_thumb_paths("_없는상품") is None

        (CUTS_DIR / g / "thumb_00.jpg").unlink()
        assert chosen_thumb_path(g) is None, "파일이 사라진 대표이미지를 등록에 넘김"
    finally:
        shutil.rmtree(CUTS_DIR / g, ignore_errors=True)

    print("thumbs._demo self-check OK")


if __name__ == "__main__":
    _demo()
