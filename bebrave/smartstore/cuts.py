# -*- coding: utf-8 -*-
"""도매 상세 이미지를 컷 단위로 쪼개고, 상품 사진에서 페이지 팔레트를 뽑는다.

왜 필요한가 — 도매매는 "완성된 상세페이지"를 세로로 아주 긴 이미지 한 장으로 준다
(실측: 주방 주걱 860x10200, 우산 800x21075). 지금까지는 이걸 통째로 상세설명에 붙여서
우리 페이지의 3/4이 공급사가 만든 페이지였고, 그 안에 소비자에게 나가면 안 되는 것
(공급사 브랜드·시험성적서·"LOGO 인쇄주문가능" 같은 B2B 안내)까지 섞여 나갔다.

이 모듈은 계산만 한다 — AI를 쓰지 않는다. 컷의 내용을 판독하고 쓸 것을 고르는 일은
cut_reader.py가 맡는다. 여기서 나온 컷은 판독 결과가 없어도 비율·크기만으로 배치할 수
있어서, 판독이 실패해도 페이지는 만들어진다.

새 의존성을 넣지 않으려고 numpy 대신 Pillow만 쓴다. 가로줄마다 "옆 칸끼리 밝기 차이"로
여백을 판정하고, 여백 한가운데서 자르며, 여백 없이 맞붙은 긴 덩어리는 이음매에서 한 번 더
자른다(slice_bounds 참고). 예전의 "폭 16칸으로 줄여 분산 보기"는 글자 한 줄을 여백으로 봐서
제목 한가운데를 잘랐다(2026-09 교체, 실측: 일자손톱깎이 16컷→26컷, 우산 최대 3,548px→1,312px).
"""
from __future__ import annotations

import colorsys
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

CUTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cuts"

# 이 비율(세로/가로)을 넘으면 "여러 컷이 이어 붙은 긴 상세 이미지"로 본다.
# 실측: 대표 사진 1.0 내외, 도매 상세 이미지 11.9(주걱)·26.3(우산).
LONG_IMAGE_RATIO = 2.5
_MIN_CUT_HEIGHT = 260   # 이보다 얇은 조각은 구분선·여백으로 보고 앞 컷에 붙인다
_BLANK_RUN = 30         # 여백으로 인정할 최소 연속 줄 수 — 14줄이면 아이콘과 이름표가 갈라졌다
_BLANK_TOL = 6          # 옆 칸끼리 밝기 차이가 이보다 작으면 여백 줄
_EDGE_COLS = 160        # 판정용으로 줄이는 가로 칸 수 — 16칸이면 글자 한 줄이 평균에 묻혔다
_MAX_CUT_HEIGHT = 1600  # 이보다 긴 덩어리는 사진끼리 맞붙은 이음매에서 한 번 더 자른다
_SEAM_MIN = 45          # 이음매로 인정할 위아래 줄의 평균 밝기 변화


@dataclass
class Cut:
    """상세 이미지에서 잘라낸 컷 한 장.

    src/y0/y1은 이 컷이 원본 어느 위치에서 잘려 나왔는지다 — 여백 기준 분할은 가끔
    아이콘 묶음이나 문단 한가운데를 자르는데(실측: 우산 기능 아이콘 6개 중 3개에서
    끊김), 그때 사람이 원본에서 범위를 다시 잡을 수 있어야 한다."""
    index: int
    filename: str
    width: int
    height: int
    source: str = "domemae"   # domemae | seller (판매자가 올린 사진)
    src: int = -1             # 몇 번째 원본 이미지에서 나왔는가
    y0: int = 0               # 원본에서의 시작 높이
    y1: int = 0               # 원본에서의 끝 높이

    @property
    def ratio(self) -> float:
        return self.height / self.width if self.width else 0.0


@dataclass
class CutSet:
    goods_no: str
    cuts: List[Cut] = field(default_factory=list)
    palette: List[dict] = field(default_factory=list)
    built_at: str = ""

    def dir(self) -> Path:
        return CUTS_DIR / self.goods_no

    def path_of(self, cut: Cut) -> Path:
        return self.dir() / cut.filename


# ── 분할 ────────────────────────────────────────────────────────────────
def _gray_rows(im: "Image.Image") -> bytes:
    return im.convert("L").resize((_EDGE_COLS, im.height), Image.BOX).tobytes()


def _row_edges(px: bytes, h: int) -> List[int]:
    """가로줄마다 옆 칸끼리 밝기 차이의 최댓값 — 값이 작을수록 여백에 가깝다.

    예전엔 폭을 16칸으로 줄여 분산을 봤는데, 한 칸이 50px를 넘어 글자 한 줄이 평균에 묻혀
    여백으로 잡혔다. 옆 칸끼리의 차이는 좌우로 은은하게 변하는 바탕에선 작고, 글자·사진
    경계에선 크다."""
    c = _EDGE_COLS
    return [max(abs(px[y * c + i + 1] - px[y * c + i]) for i in range(c - 1)) for y in range(h)]


def _seam(px: bytes, y: int) -> float:
    """y줄과 그 아랫줄이 가로 전체에서 얼마나 확 바뀌나 — 사진끼리 맞붙은 이음매는 크다."""
    c, a, b = _EDGE_COLS, y * _EDGE_COLS, (y + 1) * _EDGE_COLS
    return sum(abs(px[a + i] - px[b + i]) for i in range(c)) / c


def slice_bounds(im: "Image.Image", min_h: int = _MIN_CUT_HEIGHT, gap: int = _BLANK_RUN,
                 tol: int = _BLANK_TOL, max_h: int = _MAX_CUT_HEIGHT,
                 seam_min: float = _SEAM_MIN) -> List[Tuple[int, int]]:
    """긴 이미지를 컷 경계 목록으로 쪼갠다. 반환은 [(y0, y1), ...] — 빈틈 없이 이어진다.

    1) 여백이 gap줄 이상 이어진 곳의 한가운데서 자른다. 예전엔 여백이 시작되는 첫 줄에서
       끊어 글자 바로 밑 흐릿한 가장자리가 잘려 나갔다(일자손톱깎이 제목들).
    2) 그래도 max_h보다 긴 덩어리는 사진끼리 맞붙은 이음매에서 한 번 더 자른다 — 여백 없이
       붙은 사진 모음이 3,500px 한 컷이 되던 것(우산).
    """
    h = im.height
    px = _gray_rows(im)
    edges = _row_edges(px, h)
    out, run, start = [], 0, 0
    for y in range(h):
        if edges[y] < tol:
            run += 1
            continue
        if run >= gap and y - run - start >= min_h:
            mid = y - run // 2
            out.append((start, mid))
            start = mid
        run = 0
    if h - start >= min_h or not out:
        out.append((start, h))
    else:
        out[-1] = (out[-1][0], h)      # 짧은 꼬리는 앞 컷에 붙인다

    final, stack = [], list(reversed(out))
    while stack:
        a, b = stack.pop()
        if b - a <= max_h:
            final.append((a, b))
            continue
        best, at = max(((_seam(px, y), y) for y in range(a + min_h, b - min_h)), default=(0.0, -1))
        if best < seam_min:
            final.append((a, b))       # 이음매가 뚜렷하지 않으면 사진 한가운데를 가르지 않는다
            continue
        stack.append((at + 1, b))
        stack.append((a, at + 1))
    return final


# ── 팔레트 ──────────────────────────────────────────────────────────────
def extract_palette(im: "Image.Image", k: int = 6) -> List[dict]:
    """상품 사진에서 페이지에 쓸 색을 뽑는다 — 템플릿은 하나인데 상품마다 색이 달라진다.

    배경이 대부분 흰색이라 그냥 최빈색을 쓰면 어떤 상품이든 흰색이 나온다. 무채색과
    너무 밝거나 어두운 색을 후보에서 빼고, 그래도 남는 게 없으면(검정 우산처럼 상품
    자체가 무채색인 경우) 기준을 풀어 다시 고른다.
    """
    im = im.convert("RGB").copy()
    im.thumbnail((220, 220))
    q = im.quantize(colors=32, method=Image.MEDIANCUT).convert("RGB")
    counts = Counter(q.getdata())

    def pick(min_s: float, lo_v: float, hi_v: float) -> list:
        out = []
        for (r, g, b), n in counts.most_common(32):
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if s >= min_s and lo_v < v < hi_v:
                out.append(((r, g, b), n, h, s, v))
        return out

    cands = pick(0.18, 0.15, 0.97) or pick(0.0, 0.12, 0.90)
    if not cands:
        return []
    total = sum(c[1] for c in cands)
    cands.sort(key=lambda c: -c[1])
    return [{"hex": "#%02x%02x%02x" % c[0], "share": round(c[1] / total, 3),
             "h": round(c[2], 3), "s": round(c[3], 2), "v": round(c[4], 2)}
            for c in cands[:k]]


def page_colors(palette: List[dict]) -> dict:
    """추출색에서 페이지 전체 색을 유도한다 — 상품마다 다르되 항상 조화롭게.

    어두운 상품(검정·남색 우산)은 뽑힌 색을 그대로 배경에 쓰면 페이지가 칙칙해지므로
    채도와 명도에 하한을 둔다.
    """
    def hx(h, s, v):
        r, g, b = colorsys.hsv_to_rgb(h, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

    if not palette:   # 팔레트를 못 뽑은 상품 — 무채색 기본값으로 떨어진다
        return {"base": "#3d4450", "deep": "#1f242c", "ink": "#14171c",
                "paper": "#fafafa", "soft": "#f0f1f3", "accent": "#c8553d"}

    base = palette[0]
    far, best = None, -1.0
    for c in palette[1:]:
        d = abs(c["h"] - base["h"])
        d = min(d, 1 - d)
        if d > best:
            best, far = d, c
    if far is None or best < 0.08:
        far = {"h": (base["h"] + 0.5) % 1, "s": base["s"], "v": base["v"]}

    bh, bs, bv = base["h"], max(base["s"], 0.45), max(base["v"], 0.52)
    return {
        "base": hx(bh, bs, bv),
        "deep": hx(bh, min(bs + 0.1, 0.9), 0.24),
        "ink": hx(bh, min(bs, 0.5), 0.11),
        "paper": hx(bh, 0.04, 0.985),
        "soft": hx(bh, 0.10, 0.955),
        "accent": hx(far["h"], max(far["s"], 0.5), max(far["v"], 0.58)),
    }


# ── 빌드 / 캐시 ─────────────────────────────────────────────────────────
def _fetch(url: str) -> Optional["Image.Image"]:
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        import io
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"  [경고] 이미지 내려받기 실패: {url[:60]} ({e})")
        return None


def load_cuts(goods_no: str) -> Optional[CutSet]:
    """잘라둔 컷을 읽는다. 없거나 깨졌으면 None.

    기한을 두지 않는다(2026-09) — 저장된 AI 버전의 블록이 컷 번호로 사진을 가리키고
    사람이 범위를 고친 컷도 여기 있어서, 기한이 지나 다시 자르면 AI 버전이 원본으로
    조용히 돌아가거나 고친 범위가 날아간다.
    """
    # 도매 이미지 목록이 바뀐 경우의 다시 자르기는 build_cuts가 _same_sources로 판단한다
    meta = CUTS_DIR / str(goods_no) / "meta.json"
    if not meta.exists():
        return None
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
        return CutSet(goods_no=d["goods_no"], built_at=d.get("built_at", ""),
                      palette=d.get("palette", []),
                      cuts=[Cut(**c) for c in d.get("cuts", [])])
    except Exception:
        return None


def build_cuts(goods_no: str, image_urls: List[str], force: bool = False) -> CutSet:
    """도매 이미지들을 내려받아 컷으로 쪼개고 저장한다.

    image_urls[0]은 대표 사진으로 보고 팔레트를 여기서 뽑는다. 나머지 중 세로로 긴
    것(LONG_IMAGE_RATIO 초과)은 컷으로 쪼개고, 짧은 것은 그대로 한 컷이 된다.
    """
    if not _HAS_PIL:
        raise NotImplementedError("pip3 install Pillow 후 재시도하세요.")
    goods_no = str(goods_no)
    old = load_cuts(goods_no)
    if not force and old and _same_sources(goods_no, image_urls):
        return old

    out_dir = CUTS_DIR / goods_no
    out_dir.mkdir(parents=True, exist_ok=True)
    # 판매자가 올린 사진은 도매 컷을 다시 잘라도 남긴다 — 예전엔 폴더의 jpg를 전부 지웠다
    seller = [c for c in (old.cuts if old else []) if c.source == "seller"]
    for f in out_dir.glob("*.jpg"):
        if not f.name.startswith("seller_"):
            f.unlink()
    # 컷 번호가 바뀌면 이전 판독·문구·구성이 엉뚱한 사진을 가리킨다 — 버린다
    for stale in ("reading.json", "copy.json", "blocks.json"):
        (out_dir / stale).unlink(missing_ok=True)

    cs = CutSet(goods_no=goods_no, built_at=time.strftime("%Y-%m-%dT%H:%M"))
    idx = 0
    for n, url in enumerate(image_urls):
        im = _fetch(url)
        if im is None:
            continue
        if n == 0:
            cs.palette = extract_palette(im)
        # 원본을 남겨둔다 — 컷이 잘못 잘렸을 때 사람이 범위를 다시 잡으려면 원본이 있어야
        # 하고, 그때마다 공급사 서버에서 다시 받아오면 느리고 실패할 수도 있다.
        im.save(out_dir / f"source_{n}.jpg", quality=88)
        pieces = (slice_bounds(im) if im.height / im.width > LONG_IMAGE_RATIO
                  else [(0, im.height)])
        for y0, y1 in pieces:
            piece = im.crop((0, y0, im.width, y1))
            name = f"{idx:03d}.jpg"
            piece.save(out_dir / name, quality=86)
            cs.cuts.append(Cut(index=idx, filename=name, width=piece.width, height=piece.height,
                               src=n, y0=y0, y1=y1))
            idx += 1

    cs.cuts.extend(seller)
    (out_dir / "meta.json").write_text(
        json.dumps({"goods_no": cs.goods_no, "built_at": cs.built_at, "palette": cs.palette,
                    "sources": _source_keys(image_urls),
                    "cuts": [asdict(c) for c in cs.cuts]}, ensure_ascii=False),
        encoding="utf-8")
    return cs


def _source_keys(image_urls: List[str]) -> List[str]:
    # 주소 뒤 ?hash= 부분은 수시로 바뀐다 — 그것만으로 다시 자르면 사람이 고친 컷 범위가 날아간다
    return [u.split("?")[0] for u in image_urls if u]


def _same_sources(goods_no: str, image_urls: List[str]) -> bool:
    """잘라둔 컷이 지금의 이미지 목록으로 만든 것인가.

    컷에 기한을 없앤 뒤로, 도매매 정보를 잘못 읽어 이미지가 빠진 채 잘린 컷(일자손톱깎이:
    대표사진 1장만)이 파싱을 고친 뒤에도 계속 쓰일 뻔했다(2026-09)."""
    try:
        d = json.loads((CUTS_DIR / str(goods_no) / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    # 출처를 기록하기 전에 자른 컷은 판단 근거가 없으니 그대로 쓴다 — 사람이 고친 범위·구성이
    # 걸려 있다. 잘린 장수로 추측하면, 받기에 실패한 공급사 공지 이미지 1장 때문에(네일아트디자인)
    # 다시 자르고 또 실패하면서 만들어 둔 AI 버전만 지우게 된다.
    if "sources" not in d:
        return True
    # 받기에 실패한 이미지까지 목록째 기록하므로, 실패만으로 다시 자르지 않는다
    return d["sources"] == _source_keys(image_urls)


def register_seller_image(goods_no: str, image_bytes: bytes, max_px: int = 1400) -> Optional[Cut]:
    """판매자가 올린 사진을 같은 컷 창고에 넣는다.

    폰 사진은 4000px·5MB급이라 그대로 두면 네이버 업로드 상한(호출당 10MB)에 걸린다.
    EXIF 회전 정보를 반영하지 않으면 세로로 찍은 사진이 눕는다.
    """
    if not _HAS_PIL:
        raise NotImplementedError("pip3 install Pillow 후 재시도하세요.")
    import io
    from PIL import ImageOps
    goods_no = str(goods_no)
    out_dir = CUTS_DIR / goods_no
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    except Exception as e:
        print(f"  [경고] 업로드 이미지를 열 수 없음: {e}")
        return None
    im.thumbnail((max_px, max_px * 4))

    existing = sorted(out_dir.glob("seller_*.jpg"))
    name = f"seller_{len(existing):03d}.jpg"
    im.save(out_dir / name, quality=88)
    cut = Cut(index=1000 + len(existing), filename=name,
              width=im.width, height=im.height, source="seller")

    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            d = json.loads(meta_path.read_text(encoding="utf-8"))
            d.setdefault("cuts", []).append(asdict(cut))
            meta_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass   # 메타가 깨져도 파일은 남는다 — 다음 빌드에서 복구
    return cut


def source_path(goods_no: str, src: int) -> Path:
    return CUTS_DIR / str(goods_no) / f"source_{int(src)}.jpg"


def recut(goods_no: str, index: int, y0: int, y1: int) -> Optional[Cut]:
    """컷이 잘못 잘렸을 때 원본에서 범위를 다시 잡는다(2026-09).

    여백 기준 분할은 아이콘 묶음이나 문단 한가운데를 자르는 일이 있다(실측: 우산 기능
    아이콘 6개 중 3개에서 끊김). 같은 파일 이름을 덮어쓰므로 이 컷을 쓰는 블록은
    그대로 두고 사진만 바뀐다.
    """
    if not _HAS_PIL:
        raise NotImplementedError("pip3 install Pillow 후 재시도하세요.")
    cs = load_cuts(goods_no)
    if not cs:
        return None
    cut = next((c for c in cs.cuts if c.index == index), None)
    if cut is None or cut.src < 0:
        return None
    src = source_path(goods_no, cut.src)
    if not src.exists():
        return None

    im = Image.open(src).convert("RGB")
    y0 = max(0, min(int(y0), im.height - 20))
    y1 = max(y0 + 20, min(int(y1), im.height))
    piece = im.crop((0, y0, im.width, y1))
    piece.save(cs.dir() / cut.filename, quality=86)

    cut.width, cut.height, cut.y0, cut.y1 = piece.width, piece.height, y0, y1
    (cs.dir() / "meta.json").write_text(
        json.dumps({"goods_no": cs.goods_no, "built_at": cs.built_at, "palette": cs.palette,
                    "cuts": [asdict(c) for c in cs.cuts]}, ensure_ascii=False), encoding="utf-8")
    return cut


def save_seller_note(goods_no: str, text: str) -> None:
    """판매자가 상품에 덧붙인 메모 — 문구를 쓸 때 사실로 취급한다."""
    d = CUTS_DIR / str(goods_no)
    d.mkdir(parents=True, exist_ok=True)
    (d / "note.txt").write_text(text.strip()[:2000], encoding="utf-8")


def load_seller_note(goods_no: str) -> str:
    p = CUTS_DIR / str(goods_no) / "note.txt"
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def seller_cuts(goods_no: str) -> List[Cut]:
    """판매자가 올린 사진만. 화면에서 목록을 보여줄 때 쓴다."""
    cs = load_cuts(goods_no)
    return [c for c in cs.cuts if c.source == "seller"] if cs else []


def drop_seller_cut(goods_no: str, filename: str) -> bool:
    """판매자가 올린 사진 한 장을 뺀다. 도매 컷은 건드리지 않는다."""
    if not filename.startswith("seller_") or "/" in filename or "\\" in filename:
        return False
    d = CUTS_DIR / str(goods_no)
    (d / filename).unlink(missing_ok=True)
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            m["cuts"] = [c for c in m.get("cuts", []) if c.get("filename") != filename]
            meta_path.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return True


def _demo() -> None:
    """실행 가능한 자체 점검 — 네트워크 없이 합성 이미지로만 돌린다."""
    if not _HAS_PIL:
        print("Pillow 없음 — 건너뜀")
        return

    # (a) 여백으로 구분된 3컷짜리 긴 이미지를 정확히 3컷으로 쪼개는가
    W, H, SEG = 200, 1800, 600
    im = Image.new("RGB", (W, H), "white")
    for i, color in enumerate([(200, 60, 60), (60, 140, 200), (240, 200, 60)]):
        for y in range(i * SEG + 40, (i + 1) * SEG - 40):
            for x in range(0, W, 3):   # 줄마다 들쭉날쭉해야 "내용 있음"으로 잡힌다
                im.putpixel((x, y), color)
    bounds = slice_bounds(im, min_h=200)
    assert len(bounds) == 3, f"여백으로 나뉜 3컷을 {len(bounds)}컷으로 쪼갬"
    assert bounds[0][0] == 0 and bounds[-1][1] == H and all(
        bounds[i][1] == bounds[i + 1][0] for i in range(len(bounds) - 1)), f"컷 사이 줄이 빠짐: {bounds}"

    # (a2) 제목 두 줄 사이 좁은 여백에선 자르지 않고, 구역 사이 넓은 여백의 한가운데서 자른다
    #      (일자손톱깎이: 제목 아래 끝이 잘려 나감 / 우산: 아이콘과 이름표가 갈라짐)
    t = Image.new("RGB", (400, 1000), "white")
    def text_line(y0, y1):
        for y in range(y0, min(y1, 1000)):
            for x in range(20, 380, 7):
                t.putpixel((x, y), (30, 30, 30))
    text_line(100, 130)
    text_line(150, 180)                        # 줄 간격 20줄 — 한 제목
    for yy in range(300, 1000, 40):
        text_line(yy, yy + 25)                 # 여백 120줄 뒤의 다음 구역
    tb = slice_bounds(t, min_h=150)
    assert len(tb) == 2, f"제목 두 줄 사이 좁은 여백에서 자름: {tb}"
    assert tb[0][1] - 180 >= 20, f"글자 바로 밑에서 잘라 여유가 없음: {tb[0]}"

    # (a3) 여백 없이 맞붙은 사진 두 장은 이음매에서 자른다(우산 3,500px 덩어리)
    top = Image.effect_noise((800, 1200), 40).point(lambda v: v // 2 + 60)
    bot = Image.effect_noise((800, 1200), 40).point(lambda v: min(255, v // 2 + 160))
    stacked = Image.new("L", (800, 2400))
    stacked.paste(top, (0, 0))
    stacked.paste(bot, (0, 1200))
    sb = slice_bounds(stacked.convert("RGB"))
    assert len(sb) == 2 and abs(sb[0][1] - 1200) <= 2, f"맞붙은 사진 두 장을 이음매에서 못 자름: {sb}"
    flat = Image.effect_noise((800, 2400), 40).convert("RGB")
    assert len(slice_bounds(flat)) == 1, "이음매가 없는 긴 사진 한가운데를 가름"

    # (b) 긴 이미지 판별 — 대표 사진은 쪼개지 않는다
    assert (1800 / 200) > LONG_IMAGE_RATIO
    assert (760 / 760) < LONG_IMAGE_RATIO, "정사각 대표 사진이 분할 대상으로 잡힘"

    # (c) 팔레트가 흰 배경이 아니라 상품 색을 잡는가
    pal = extract_palette(im)
    assert pal, "상품 색을 하나도 못 뽑음"
    assert all(p["hex"] not in ("#ffffff", "#fefefe") for p in pal), "배경 흰색이 상품 색으로 잡힘"

    # (d) 상품이 무채색이어도 페이지 색이 나오는가 (검정 우산 사례)
    gray = Image.new("RGB", (100, 100), (40, 40, 42))
    colors = page_colors(extract_palette(gray))
    assert set(colors) == {"base", "deep", "ink", "paper", "soft", "accent"}
    assert colors["paper"] != colors["ink"], "배경과 글자색이 같아 글씨가 안 보임"

    # (e) 팔레트를 아예 못 뽑아도 페이지 색은 성립해야 한다
    fallback = page_colors([])
    assert fallback["paper"] != fallback["ink"]

    # (f) 잘못 잘린 컷을 원본에서 다시 잡으면 그만큼 늘어난다
    import shutil
    d = CUTS_DIR / "_demo_recut"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    im.save(d / "source_0.jpg", quality=88)
    im.crop((0, 0, W, 400)).save(d / "000.jpg", quality=88)   # 일부러 짧게 자른 컷
    (d / "meta.json").write_text(json.dumps({
        "goods_no": "_demo_recut", "built_at": "", "palette": [],
        "cuts": [{"index": 0, "filename": "000.jpg", "width": W, "height": 400,
                  "source": "domemae", "src": 0, "y0": 0, "y1": 400}]}, ensure_ascii=False),
        encoding="utf-8")
    fixed = recut("_demo_recut", 0, 0, 1200)
    assert fixed is not None and fixed.height == 1200, f"범위를 다시 잡았는데 높이가 안 늘어남: {fixed}"
    assert Image.open(d / "000.jpg").height == 1200, "컷 파일이 새 범위로 안 바뀜"
    assert recut("_demo_recut", 99, 0, 100) is None, "없는 컷 번호가 통과함"
    shutil.rmtree(d, ignore_errors=True)

    # 잘라둔 컷이 지금 이미지 목록과 맞는지 — 이미지가 빠진 채 잘린 컷을 계속 쓰지 않는다
    import shutil as _sh
    dd = CUTS_DIR / "_demo_src"
    dd.mkdir(parents=True, exist_ok=True)
    try:
        def meta(obj):
            (dd / "meta.json").write_text(json.dumps(obj), encoding="utf-8")
        urls = ["https://a/img_760?hash=1", "https://b/01.jpg", "https://b/02.jpg"]
        meta({"sources": ["https://a/img_760", "https://b/01.jpg", "https://b/02.jpg"], "cuts": []})
        assert _same_sources("_demo_src", ["https://a/img_760?hash=2"] + urls[1:]), \
            "주소 뒤 hash만 바뀌었는데 다시 자름 — 사람이 고친 컷 범위가 날아감"
        assert not _same_sources("_demo_src", urls[:1]), "이미지 목록이 바뀌었는데 옛 컷을 씀"
        # 출처 기록 전에 자른 옛 컷 — 원본 6장만 잘렸어도(7번째는 받기 실패) 그대로 쓴다
        meta({"cuts": [{"src": 0}, {"src": 5}]})
        assert _same_sources("_demo_src", urls + ["https://c/notice.jpg"] * 5), \
            "근거 없이 옛 컷을 다시 잘라 사람이 만든 AI 버전을 지움"
    finally:
        _sh.rmtree(dd, ignore_errors=True)

    print("cuts._demo self-check OK")


if __name__ == "__main__":
    _demo()
