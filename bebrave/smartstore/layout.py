# -*- coding: utf-8 -*-
"""상세페이지를 구간 단위로 조립한다 (2026-09).

구간 순서는 구매자가 머릿속에서 묻는 순서를 따른다 — 이게 뭔지(첫 화면) → 나한테
필요한가(공감) → 뭐가 좋은데(핵심 3가지 → 장점) → 어떻게 쓰나(사용 장면) → 뭘 고르지
(선택지) → 정확히 어떤 물건인가(크기·사양) → 사도 되나(배송·교환).

근거 두 가지:
 - 이탈은 첫 화면에서 갈린다. 그래서 결론(핵심 3가지)을 첫 스크롤 안에 둔다.
 - 상세 정보가 시작되는 구간부터 이탈이 치솟는다. 그래서 치수·사양을 뒤로 뺐다.

이미지는 고정 비율 틀에 넣지 않는다. 도매 컷은 비율이 0.35~4.39로 제각각이고 피사체가
프레임의 50~90%를 채워서, 어느 방향으로 잘라도 상품이 잘린다(실측: 예전 시안에서 최대
50% 잘림). 원본 비율 그대로 싣는 것 외에 답이 없다.

<style> 태그·웹폰트·자바스크립트는 스마트스토어에서 통하지 않는다고 보고 전부 인라인
스타일로 짠다. 미디어쿼리를 못 쓰므로 반응형은 clamp()와 flex-wrap으로 처리한다.
"""
from __future__ import annotations

import html
import re
import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .cuts import CutSet, page_colors
from .cut_reader import Reading

_MODEL = "claude-haiku-4-5-20251001"   # 문구는 이미 읽어낸 사실을 재배열하는 일이라 Haiku로 충분


@dataclass
class Copy:
    """페이지에 들어가는 글. 재료가 없으면 해당 항목이 비고, 그 구간은 통째로 빠진다."""
    title: str = ""
    lead: str = ""
    empathy: str = ""
    keys: List[Tuple[str, str]] = field(default_factory=list)      # (짧은 제목, 한 줄 설명)
    points: List[Tuple[str, str]] = field(default_factory=list)    # (제목, 본문)
    usecase: List[str] = field(default_factory=list)
    # 각 point에 붙일 컷 번호. 비면 배치기가 알아서 고르는데, 그러면 "이음매가 없습니다"
    # 아래에 열탕소독 컷이 붙는 식으로 글과 사진이 어긋난다(2026-09 발견).
    point_cuts: List[int] = field(default_factory=list)
    fallback_reason: str = ""   # 기본 문구로 떨어진 이유 — 화면에 보여준다


def esc(s) -> str:
    return html.escape(str(s)) if s else ""


_EDIT = False   # render_blocks(editable=True) 동안만 켜진다


def fld(text, name: str) -> str:
    """편집 모드에서 이 글자가 어느 칸인지 표시한다 — 화면에서 바로 고칠 수 있게.
    편집 모드가 아니면 평범한 글자로 나가므로 실제 상세페이지에는 흔적이 남지 않는다."""
    if _EDIT:
        return (f'<span data-f="{name}" style="display:inline-block;min-width:1em;">'
                f'{esc(text)}</span>')
    return esc(text)


# ── 문구 ────────────────────────────────────────────────────────────────
_COPY_PROMPT = """네이버 스마트스토어 상세페이지에 들어갈 글을 써라.

상품명: {name}
카테고리: {category}
상세 이미지에서 읽어낸 사실:
{facts}

쓸 수 있는 컷(번호: 그 컷에 적힌 글자 / 종류):
{cutlist}
{note}
다음을 JSON으로만 출력해라.
- title: 상품을 부르는 짧은 이름. 12자 이내. 키워드 나열 말고 사람이 부르는 말로.
- lead: 첫 화면 배지에 넣을 한 마디. 10자 안팎.
- empathy: 이 상품을 찾는 사람이 겪는 불편을 두 문장으로. 과장하지 말고 담담하게.
  줄바꿈은 \\n 하나로 표시.
- keys: 핵심 3가지. 각각 [짧은 제목(6자 이내), 한 줄 설명(15자 이내)].
- points: 장점 3가지. 각각 [제목(문장형, 20자 이내), 본문(두 문장, 각 40자 안팎)].
- usecase: 이 상품을 쓰는 상황 4가지. 각각 12자 이내.
- point_cuts: points 각 항목에 붙일 컷 번호 3개. 그 글과 실제로 같은 내용을 보여주는
  컷을 골라라. 맞는 컷이 없으면 -1.

규칙
- 위에 적힌 사실 밖으로 나가지 마라. 없는 효능·인증·수치를 지어내지 마라.
- "최고", "1위", "완벽" 같은 과장 금지. 의학적 효과를 말하지 마라.
- 사실이 모자라면 그만큼만 써라. 억지로 채우지 마라.
- 공급사·도매·다른 쇼핑몰을 언급하지 마라.

JSON만 출력해라."""


def _fallback_copy(product, facts: List[str]) -> Copy:
    """키가 없거나 실패했을 때 — 지어내지 않고 있는 것만 배치한다."""
    from .name_optimizer import _find_mood_word
    mood = _find_mood_word(getattr(product, "category", "") or "")
    head = (getattr(product, "name", "") or "").split()
    title = " ".join(head[:3])[:20] or "상품"
    keys = [(f[:6], f[:15]) for f in facts[:3]]
    points = [(f[:20], f[:80]) for f in facts[:3]]
    return Copy(title=title, lead=mood or "", empathy="", keys=keys, points=points, usecase=[])


def _copy_path(goods_no: str):
    from .cuts import CUTS_DIR
    return CUTS_DIR / str(goods_no) / "copy.json"


def save_copy(goods_no: str, c: Copy) -> None:
    """문구를 저장한다. API 키 없이 사람이(또는 대화 중인 모델이) 써 넣은 문구도
    이 파일에 담기고, 그러면 다음부터 그대로 쓰인다 — 추가 호출이 없다."""
    from dataclasses import asdict
    p = _copy_path(goods_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(c), ensure_ascii=False), encoding="utf-8")


def load_copy(goods_no: str) -> Optional[Copy]:
    p = _copy_path(goods_no)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return Copy(title=d.get("title", ""), lead=d.get("lead", ""), empathy=d.get("empathy", ""),
                    keys=[tuple(x) for x in d.get("keys", [])],
                    points=[tuple(x) for x in d.get("points", [])],
                    usecase=d.get("usecase", []),
                    point_cuts=[int(i) for i in d.get("point_cuts", [])])
    except Exception:
        return None


def _copy_from_json(d: dict) -> Copy:
    """AI 답을 Copy로 옮기며 한 번 더 거른다 — 프롬프트의 과장 금지를 믿지 않는다.

    생각 단계를 끄면 빨라지는 대신 금지한 "완벽 차단"을 써 넣었고, 장점별 사진 번호를
    [0, 6, 10]처럼 목록으로 줘서 통째로 버려졌다(2026-09 실측)."""
    from .content import sanitize_detail_html

    def clean(v, n):
        t = sanitize_detail_html(str(v or ""))
        return re.sub(r"[ \t]{2,}", " ", t).strip()[:n]

    def pairs(key, n):
        return [(clean(row[0], 40), clean(row[1], 200)) for row in (d.get(key) or [])[:n]
                if isinstance(row, (list, tuple)) and len(row) >= 2]

    def first_int(v):
        if isinstance(v, (list, tuple)):
            v = v[0] if v else -1
        return int(v) if str(v).lstrip("-").isdigit() else None

    cuts = [first_int(i) for i in (d.get("point_cuts") or [])[:3]]
    return Copy(title=clean(d.get("title"), 30), lead=clean(d.get("lead"), 20),
                empathy=clean(d.get("empathy"), 200), keys=pairs("keys", 3), points=pairs("points", 3),
                usecase=[clean(u, 20) for u in (d.get("usecase") or [])[:4]],
                point_cuts=[i for i in cuts if i is not None])


def write_copy(product, facts: List[str], seller_note: str = "", force: bool = False,
               reading=None) -> Copy:
    """읽어낸 사실로 페이지 문구를 쓴다. 저장된 문구가 있으면 그걸 쓰고, Claude를 못 쓰면 폴백."""
    goods_no = getattr(product, "goods_no", "")
    if not force and goods_no:
        saved = load_copy(goods_no)
        if saved and saved.title:
            return saved

    def fall(reason: str) -> Copy:
        c = _fallback_copy(product, facts)
        c.fallback_reason = reason
        return c

    if not facts:
        return fall("사진에서 읽어낸 사실이 없습니다")

    note = f"\n판매자가 덧붙인 메모(사실로 취급해라):\n{seller_note}\n" if seller_note.strip() else ""
    cutlist = "\n".join(
        f"- {r.index}: {r.text or '(글자 없음)'} / {r.kind}"
        for r in (reading.reads if reading else []) if r.use) or "- (없음)"
    prompt = _COPY_PROMPT.format(
        name=getattr(product, "name", ""), category=getattr(product, "category", ""),
        facts="\n".join(f"- {f}" for f in facts), cutlist=cutlist, note=note)
    # 이 Mac의 Claude Code(지금 쓰는 구독)로 쓴다 — API 키·별도 결제 없음(claude_cli.py)
    from .claude_cli import ClaudeUnavailable, ask
    import re as _re
    try:
        m = _re.search(r"\{.*\}", ask(prompt, model=_MODEL, timeout=240, think=False), _re.S)
        d = json.loads(m.group(0))
    except ClaudeUnavailable as e:
        print(f"  [경고] 상세페이지 문구 생성 실패 — 기본 문구로 대체합니다 ({e})")
        return fall(str(e))
    except (AttributeError, ValueError):
        return fall("문구 결과를 읽을 수 없습니다")

    c = _copy_from_json(d)
    if not c.title:
        c.title = _fallback_copy(product, facts).title
    if goods_no:
        save_copy(goods_no, c)   # 다음부터는 호출 없이 이 문구를 쓴다
    return c


# ── 컷 배정 ─────────────────────────────────────────────────────────────
class _Picker:
    """컷을 한 번씩만 쓰도록 나눠준다. 판매자 사진을 도매 컷보다 먼저 준다 —
    실물 사진은 경쟁 셀러와 겹치지 않는 유일한 자산이다."""

    def __init__(self, cs: CutSet, reading: Reading):
        self.cs, self.reading, self.used = cs, reading, set()
        self.by_index = {c.index: c for c in cs.cuts}
        # 판독이 추천한 순서 → 가치 점수 → 원래 순서로 고른다. 가치 판단이 없는 예전 판독은 원래 순서 그대로.
        rank = {i: k for k, i in enumerate(reading.order)}
        self.ranked = sorted((c.index for c in cs.cuts),
                             key=lambda i: (rank.get(i, 10_000), -reading.of(i).value, i))

    def _ok(self, idx: int) -> bool:
        # 초안에 넣을 컷만 — 겹치거나 정보가 적은 컷은 사진 고르기 목록에만 남는다(Reading.in_draft)
        return idx not in self.used and self.reading.in_draft(idx)

    def take(self, kind: Optional[str] = None, seller_first: bool = True) -> Optional[int]:
        pool = [i for i in self.ranked if self._ok(i)]
        if seller_first:
            seller = [i for i in pool if self.by_index[i].source == "seller"]
            if seller:
                self.used.add(seller[0])
                return seller[0]
        if kind:
            match = [i for i in pool if self.reading.of(i).kind == kind]
            if match:
                self.used.add(match[0])
                return match[0]
            return None
        if pool:
            self.used.add(pool[0])
            return pool[0]
        return None

    def claim(self, idx: int) -> bool:
        """문구가 지정한 컷을 찍어서 가져온다. 이미 쓰였거나 내보낼 수 없으면 실패."""
        if idx in self.by_index and self._ok(idx):
            self.used.add(idx)
            return True
        return False

    def take_any(self, *kinds: str) -> Optional[int]:
        for k in kinds:
            got = self.take(kind=k, seller_first=False)
            if got is not None:
                return got
        return self.take(seller_first=False)

    def labeled(self) -> List[Tuple[int, str]]:
        """색상명 같은 라벨이 붙은 컷들 — 선택지 구간에 쓴다."""
        out = [(c.index, self.reading.of(c.index).label) for c in self.cs.cuts
               if self._ok(c.index) and self.reading.of(c.index).label]
        for i, _ in out:
            self.used.add(i)
        return out


# ── 조각 ────────────────────────────────────────────────────────────────
def _img(src: str) -> str:
    """잘림 0 — 고정 비율 틀 없이 원본 비율 그대로."""
    return (f'<img src="{esc(src)}" alt="" loading="lazy" '
            f'style="width:100%;height:auto;display:block;border:0;">')

def _eyebrow(t: str, c: str, mb: int = 14) -> str:
    return (f'<div style="font-size:11px;font-weight:800;letter-spacing:.24em;'
            f'color:{c};margin-bottom:{mb}px;">{esc(t)}</div>')


def _sec_hero(src: str, cp: Copy, P: dict) -> str:
    if not src:
        return ""
    badge = (f'<div style="display:inline-block;background:{P["accent"]};color:#fff;font-size:11.5px;'
             f'font-weight:800;letter-spacing:.18em;padding:8px 14px;margin-bottom:16px;">'
             f'{esc(cp.lead)}</div>') if cp.lead else ""
    return (f'<div style="position:relative;background:{P["ink"]};">{_img(src)}'
            f'<div style="position:absolute;left:0;right:0;bottom:0;padding:clamp(24px,5vw,52px);'
            f'background:linear-gradient(transparent,rgba(0,0,0,.32) 22%,rgba(0,0,0,.85));">{badge}'
            f'<div style="color:#fff;font-size:clamp(34px,8.6vw,68px);font-weight:900;'
            f'letter-spacing:-.05em;line-height:1.03;">{fld(cp.title, "title")}</div></div></div>')


def _sec_empathy(cp: Copy, P: dict) -> str:
    if not cp.empathy.strip():
        return ""
    lines = fld(cp.empathy, "title") if _EDIT else "<br>".join(
        esc(l) for l in cp.empathy.split("\n") if l.strip())
    return (f'<div style="background:{P["paper"]};padding:clamp(52px,9vw,92px) clamp(22px,5vw,48px);">'
            f'<div style="max-width:600px;margin:0 auto;text-align:center;">'
            f'<div style="width:40px;height:4px;background:{P["accent"]};margin:0 auto 30px;"></div>'
            f'<div style="font-size:clamp(19px,3.7vw,28px);font-weight:700;line-height:1.8;'
            f'letter-spacing:-.03em;color:{P["ink"]};">{lines}</div></div></div>')


def _sec_keys(cp: Copy, P: dict) -> str:
    if not cp.keys:
        return ""
    cells = "".join(
        f'<div style="flex:1 1 168px;min-width:150px;text-align:center;padding:20px 12px;">'
        f'<div style="font-size:clamp(16px,3.2vw,20px);font-weight:900;color:#fff;'
        f'letter-spacing:-.03em;margin-bottom:6px;">{esc(t)}</div>'
        f'<div style="font-size:13px;color:rgba(255,255,255,.66);line-height:1.55;">{esc(d)}</div></div>'
        for t, d in cp.keys)
    return (f'<div style="background:{P["deep"]};padding:clamp(18px,3.5vw,30px) clamp(14px,3vw,32px);">'
            f'<div style="display:flex;flex-wrap:wrap;max-width:800px;margin:0 auto;">{cells}</div></div>')


def _sec_point(n: int, title: str, body: str, src: Optional[str], P: dict, dark: bool) -> str:
    bg = P["base"] if dark else P["paper"]
    fg = "#fff" if dark else P["ink"]
    sub = "rgba(255,255,255,.85)" if dark else "rgba(0,0,0,.58)"
    pic = _img(src) if src else ""
    return (f'<div style="background:{bg};">'
            f'<div style="padding:clamp(44px,7.5vw,76px) clamp(22px,5vw,48px) clamp(26px,4.5vw,40px);'
            f'max-width:720px;margin:0 auto;">'
            f'<div style="font-size:11px;font-weight:800;letter-spacing:.24em;color:{sub};'
            f'margin-bottom:12px;">POINT {n}</div>'
            f'<div style="font-size:clamp(25px,5.2vw,40px);font-weight:900;letter-spacing:-.045em;'
            f'line-height:1.2;color:{fg};margin-bottom:14px;">{fld(title, "title")}</div>'
            f'<div style="font-size:15px;line-height:1.9;color:{sub};">{fld(body, "body")}</div></div>{pic}</div>')


def _sec_usecase(cp: Copy, src: Optional[str], P: dict) -> str:
    if not cp.usecase:
        return ""
    items = "".join(
        f'<div style="flex:1 1 176px;min-width:150px;border-top:2px solid {P["accent"]};'
        f'padding-top:13px;font-size:15px;font-weight:700;color:{P["ink"]};line-height:1.5;">{esc(u)}</div>'
        for u in cp.usecase)
    pic = _img(src) if src else ""
    return (f'<div style="background:{P["soft"]};">{pic}'
            f'<div style="padding:clamp(38px,6.5vw,64px) clamp(22px,5vw,48px);max-width:820px;margin:0 auto;">'
            f'{_eyebrow("이럴 때 씁니다", P["accent"])}'
            f'<div style="display:flex;gap:clamp(14px,3vw,26px);flex-wrap:wrap;">{items}</div></div></div>')


def _sec_choice(swatches: List[Tuple[str, str]], options: List[str], P: dict) -> str:
    """색상 컷이 있으면 그리드로, 없으면 옵션 이름만. 둘 다 없으면 구간째 생략."""
    if swatches:
        # 색상 컷끼리는 비율이 거의 같아(실측 1.06~1.09) 나란히 놓아도 잘리지 않는다.
        cells = "".join(
            f'<div style="flex:1 1 148px;min-width:130px;max-width:210px;">'
            f'<div style="background:#fff;">{_img(src)}</div>'
            f'<div style="text-align:center;font-size:13px;font-weight:700;color:{P["ink"]};'
            f'margin-top:8px;">{esc(name)}</div></div>' for src, name in swatches)
        body = (f'<div style="display:flex;gap:clamp(9px,2vw,16px);flex-wrap:wrap;'
                f'justify-content:center;">{cells}</div>')
        big = (f'<div style="font-size:clamp(60px,13vw,110px);font-weight:900;color:{P["accent"]};'
               f'line-height:.9;letter-spacing:-.06em;">{len(swatches)}</div>')
        title = f"{len(swatches)}가지 중에 고르세요"
    elif len(options) >= 2:
        chips = "".join(
            f'<span style="border:1px solid rgba(0,0,0,.14);border-radius:999px;padding:9px 15px;'
            f'font-size:13.5px;color:{P["ink"]};background:#fff;">{esc(o)}</span>' for o in options[:12])
        body = (f'<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;">{chips}</div>')
        big, title = "", "선택할 수 있습니다"
    else:
        return ""
    return (f'<div style="background:{P["paper"]};padding:clamp(46px,8vw,84px) clamp(22px,5vw,48px);">'
            f'<div style="max-width:820px;margin:0 auto;text-align:center;">{big}'
            f'<div style="font-size:clamp(23px,4.6vw,35px);font-weight:900;color:{P["ink"]};'
            f'letter-spacing:-.045em;margin:{"8px" if big else "0"} 0 30px;">{esc(title)}</div>'
            f'{body}</div></div>')


def _sec_spec(product, src: Optional[str], P: dict) -> str:
    rows = [(k, v) for k, v in (
        ("원산지", getattr(product, "origin_country", "")),
        ("제조사", getattr(product, "manufacturer", "")),
        ("모델명", (getattr(product, "model", "") or "") if getattr(product, "model", "") != "해당없음" else ""),
    ) if v]
    if not rows and not src:
        return ""
    table = "".join(
        f'<div style="display:flex;padding:14px 0;border-bottom:1px solid rgba(255,255,255,.1);">'
        f'<div style="width:92px;flex-shrink:0;font-size:13px;color:rgba(255,255,255,.45);'
        f'font-weight:600;">{esc(k)}</div>'
        f'<div style="font-size:14.5px;color:rgba(255,255,255,.93);line-height:1.6;">{esc(v)}</div></div>'
        for k, v in rows)
    pic = (f'<div style="background:#fff;max-width:620px;margin:0 auto 30px;">{_img(src)}</div>') if src else ""
    return (f'<div style="background:{P["deep"]};padding:clamp(42px,7vw,72px) clamp(22px,5vw,48px);">'
            f'<div style="max-width:620px;margin:0 auto;">'
            f'{_eyebrow("크기와 사양", P["accent"], 20)}{pic}{table}</div></div>')


def _sec_gallery(srcs: List[str], P: dict) -> str:
    """구간에 배정되고 남은 컷을 순서대로 싣는다.

    판독 키가 없으면 사실을 못 읽어 장점·사용 장면 구간이 통째로 비는데, 그때 이 구간이
    페이지를 지탱한다. 키가 없어도 최소한 "잘리지 않고 공급사 주소가 새지 않는" 페이지는
    나와야 한다. 원본 비율 그대로라 어떤 컷이 와도 잘리지 않는다.
    """
    if not srcs:
        return ""
    return f'<div style="background:{P["paper"]};">{"".join(_img(s) for s in srcs)}</div>'


def _sec_policy(P: dict) -> str:
    """배송·교환 안내 — 등록 페이로드와 같은 값을 쓰도록 content._build_policy_html()을
    그대로 부른다. 두 곳이 각자 문자열을 들고 있으면 배송비가 바뀔 때 한쪽만 고치고
    잊어버리는 사고가 난다."""
    from .content import _build_policy_html
    inner = _build_policy_html()
    return f'<div style="background:{P["soft"]};">{inner}</div>'


# ── 조립 ────────────────────────────────────────────────────────────────
@dataclass
class PagePlan:
    """어느 컷을 어느 구간에 쓸지. 렌더링보다 먼저 정해야 하는 이유 —
    네이버는 호출당 10장만 받으므로, 실제로 쓸 컷만 골라 올려야 한다."""
    hero: Optional[int] = None
    points: List[Optional[int]] = field(default_factory=list)
    usecase: Optional[int] = None
    swatches: List[Tuple[int, str]] = field(default_factory=list)
    spec: Optional[int] = None
    spec_mate: Optional[int] = None    # 사양 컷과 이어지는 짝(제목·표 앞부분 등) — 사양 바로 앞에 싣는다
    gallery: List[int] = field(default_factory=list)

    def used(self) -> List[int]:
        """업로드해야 할 컷을 화면에 나오는 순서대로. 대표이미지가 될 hero가 맨 앞."""
        seq = ([self.hero] + list(self.points) + [i for i, _ in self.swatches]
               + [self.usecase] + self.gallery + [self.spec_mate, self.spec])
        out = []
        for i in seq:
            if i is not None and i not in out:
                out.append(i)
        return out

    def trim_to(self, keep: List[int]) -> "PagePlan":
        """업로드에 실패했거나 상한에 밀린 컷을 빼고 다시 짠다 — 빈 자리는 그 구간이
        통째로 빠지거나 사진 없이 글자만 나온다."""
        ok = set(keep)
        return PagePlan(
            hero=self.hero if self.hero in ok else None,
            points=[i if i in ok else None for i in self.points],
            usecase=self.usecase if self.usecase in ok else None,
            swatches=[(i, n) for i, n in self.swatches if i in ok],
            spec=self.spec if self.spec in ok else None,
            spec_mate=self.spec_mate if self.spec_mate in ok else None,
            gallery=[i for i in self.gallery if i in ok])


def plan_page(cs: CutSet, reading: Reading, cp: Copy, max_gallery: int = 20) -> PagePlan:
    """컷을 구간에 배정한다. 한 컷은 한 번만 쓴다."""
    pick = _Picker(cs, reading)
    plan = PagePlan()
    # 첫 화면 — 판매자 사진이 있으면 그걸, 없으면 판독이 고른 대표 사진. 색상 선택지가 가져가기 전에 잡는다.
    seller = [c.index for c in cs.cuts if c.source == "seller" and pick._ok(c.index)]
    if seller:
        plan.hero = seller[0]
        pick.used.add(seller[0])
    elif reading.hero >= 0 and pick.claim(reading.hero):
        plan.hero = reading.hero
    plan.swatches = pick.labeled()
    if plan.hero is None:
        plan.hero = pick.take(kind="product")
    if plan.hero is None:
        plan.hero = pick.take_any("explain", "product")
    # 문구가 컷을 지정했으면 그걸 쓴다 — 안 그러면 글과 사진이 어긋난다.
    plan.points = []
    for n in range(len(cp.points)):
        want = cp.point_cuts[n] if n < len(cp.point_cuts) else -1
        if want is not None and want >= 0 and pick.claim(want):
            plan.points.append(want)
        else:
            plan.points.append(pick.take_any("explain", "product"))
    plan.usecase = pick.take_any("product") if cp.usecase else None
    # 사양은 같은 종류 중 점수가 가장 높은 컷 — 추천 순서대로 고르니 "제품정보" 제목 컷이 치수표를
    # 밀어내고 사양 자리를 차지했다(우산 26·27번, 2026-09 실측)
    specs = [i for i in pick.ranked if pick._ok(i) and reading.of(i).kind == "spec"]
    if specs:
        plan.spec = max(specs, key=lambda i: reading.of(i).value)
        pick.used.add(plan.spec)
        mate = reading.of(plan.spec).cont
        # 짝이 2점 이상이면 사양 바로 앞에 붙인다. 1점(제목만 있는 컷)은 사양 구간 제목과 겹쳐 뺀다.
        if mate >= 0 and pick._ok(mate) and reading.of(mate).value >= 2:
            plan.spec_mate = mate
            pick.used.add(mate)
    # 남은 사진은 추천 순서대로 — 예전엔 원본 순서대로 12장이라 중요한 사진이 뒤에 있으면 빠졌다.
    rest = [i for i in pick.ranked if pick._ok(i)]
    # 짝 덕분에 남은 1점 컷(제목만 있는 컷 등)은 짝이 다른 구간으로 갔으면 싣지 않는다 — "제품정보" 제목만
    # 사진 사이에 떨어져 나오고 짝인 치수표는 사양 구간으로 갔다(우산 26·27번, 2026-09 실측).
    rest = [i for i in rest
            if not (reading.of(i).value == 1 and reading.of(i).cont >= 0 and reading.of(i).cont not in rest)]
    # 이어지는 짝은 붙여서 싣는다(원래 순서대로) — 추천 순서가 둘 사이에 다른 사진을 끼워도 문장이 끊기지 않게
    gallery = []
    for i in rest:
        if i in gallery:
            continue
        mate = reading.of(i).cont
        gallery.extend(sorted([i, mate]) if mate in rest and mate not in gallery else [i])
    plan.gallery = gallery[:max_gallery]
    return plan


def render_page(product, cs: CutSet, reading: Reading, cp: Copy, plan: PagePlan, url_of) -> str:
    """구간을 순서대로 이어 붙인다. `url_of(cut_index)`는 그 컷의 이미지 주소를 준다."""
    P = page_colors(cs.palette)
    u = lambda i: url_of(i) if i is not None else None

    points_html = ""
    for n, (title, body) in enumerate(cp.points):
        ci = plan.points[n] if n < len(plan.points) else None
        points_html += _sec_point(n + 1, title, body, u(ci), P, dark=(n % 2 == 0))

    options = [o.get("name", "") for o in (getattr(product, "options", None) or [])
               if o.get("extra_price", 0) == 0]
    body = (_sec_hero(u(plan.hero) or "", cp, P) + _sec_empathy(cp, P) + _sec_keys(cp, P)
            + points_html
            + _sec_usecase(cp, u(plan.usecase), P)
            + _sec_choice([(url_of(i), n) for i, n in plan.swatches], options, P)
            + _sec_gallery([url_of(i) for i in plan.gallery], P)
            + _sec_gallery([url_of(plan.spec_mate)] if plan.spec_mate is not None else [], P)
            + _sec_spec(product, u(plan.spec), P)
            + _sec_policy(P))
    return (f'<div style="font-family:-apple-system,BlinkMacSystemFont,\'Apple SD Gothic Neo\','
            f'\'Malgun Gothic\',sans-serif;max-width:860px;margin:0 auto;background:{P["paper"]};">'
            f'{body}</div>')


def build_page(product, cs: CutSet, reading: Reading, cp: Copy, url_of) -> str:
    """배정과 렌더링을 한 번에 — 업로드가 필요 없는 미리보기용."""
    return render_page(product, cs, reading, cp, plan_page(cs, reading, cp), url_of)


def _demo() -> None:
    """자체 점검 — 네트워크·API 호출 없음."""
    from .cuts import Cut
    from .cut_reader import CutRead

    class P:   # DomemaeProduct 대역
        name = "대코 브라이트 미니볶음주걱 실리콘 이유식주걱"
        category = "생활용품>주방용품>조리기구>주걱"
        origin_country = "국산"
        manufacturer = "서울산업"
        model = "해당없음"
        options = []
        supplier = "seoul7rose"
        stock = 999999
        supply_price = 1200

    cs = CutSet(goods_no="_demo", palette=[{"hex": "#cba451", "share": .3, "h": .11, "s": .6, "v": .8}],
                cuts=[Cut(index=i, filename=f"{i:03d}.jpg", width=800, height=800) for i in range(5)])
    reading = Reading(goods_no="_demo", by_ai=True, facts=["열탕 소독 가능", "일체형 디자인"],
                      reads=[CutRead(index=0, kind="product", use=True),
                             CutRead(index=1, kind="explain", use=True, text="열탕소독 OK"),
                             CutRead(index=2, kind="supplier", use=False, reason="LOGO 인쇄주문 안내"),
                             CutRead(index=3, kind="spec", use=True),
                             CutRead(index=4, kind="product", use=True)])
    cp = Copy(title="미니 실리콘 주걱", lead="작은 냄비에 딱", empathy="큰 주걱은 불편합니다.\n작은 게 낫습니다.",
              keys=[("열탕소독", "끓는 물에 그대로")], points=[("이음매가 없습니다", "틈이 안 생깁니다.")],
              usecase=["이유식 만들 때"])
    html_out = build_page(P(), cs, reading, cp, lambda i: f"/img/{i}.jpg")

    # (a) 공급사 자료 컷은 어떤 구간에도 들어가면 안 된다
    assert "/img/2.jpg" not in html_out, "공급사 자료 컷이 소비자 화면으로 나감"

    # (b) 고정 비율 틀(padding-bottom 트릭·object-fit)이 남아 있으면 이미지가 잘린다
    assert "object-fit" not in html_out, "이미지를 잘라내는 스타일이 남아 있음"
    assert "padding-bottom:" not in html_out, "비율 고정 틀이 남아 있음"

    # (c) 공급사명·도매 재고·매입가는 절대 본문에 나가지 않는다
    for leak in ("seoul7rose", "999999", "1200"):
        assert leak not in html_out, f"본문에 노출되면 안 되는 값이 들어감: {leak}"

    # (d) 재료가 없는 구간은 통째로 빠지고, 있는 구간은 나온다
    assert "이유식 만들 때" in html_out and "열탕소독" in html_out
    assert "가지 중에 고르세요" not in html_out, "선택지가 없는데 선택 구간이 나옴"

    # (e) 배송 안내는 등록 페이로드와 같은 출처를 쓴다
    assert "배송" in html_out

    # (f) 컷은 한 번씩만 쓰인다 — 같은 사진이 두 번 나오면 페이지가 조잡해진다
    import re as _re
    used = _re.findall(r'/img/(\d+)\.jpg', html_out)
    assert len(used) == len(set(used)), f"같은 컷이 여러 번 쓰임: {used}"

    # (g) 문구가 하나도 없어도 페이지는 만들어져야 한다
    bare = build_page(P(), cs, reading, Copy(title="주걱"), lambda i: f"/img/{i}.jpg")
    assert "주걱" in bare and len(bare) > 200

    # 가치 판단이 있으면 대표는 판독이 고른 컷, 나머지는 추천 순서, 겹치는 컷·공급사 자료는 초안에서 뺀다
    rd2 = Reading(goods_no="_demo", by_ai=True, hero=4, order=[3, 1],
                  reads=[CutRead(index=0, kind="product", use=True, value=1, dup=4),
                         CutRead(index=1, kind="explain", use=True, value=2),
                         CutRead(index=2, kind="supplier", use=False, value=3),
                         CutRead(index=3, kind="product", use=True, value=2),
                         CutRead(index=4, kind="product", use=True, value=3)])
    p2 = plan_page(cs, rd2, Copy(title="주걱"))
    assert p2.hero == 4, f"판독이 고른 대표 사진을 안 씀: {p2.hero}"
    assert p2.gallery == [3, 1], f"추천 순서대로 안 실음: {p2.gallery}"
    assert 0 not in p2.used() and 2 not in p2.used(), f"겹치는 컷·공급사 자료가 초안에 들어감: {p2.used()}"

    # 짝 컷 배치 — 짝이 사양 구간으로 간 제목 컷은 빼고, 이어지는 짝은 붙여서 싣는다(우산 26·27번 사례)
    rd3 = Reading(goods_no="_demo", by_ai=True, hero=0, order=[2, 0, 1],
                  reads=[CutRead(index=0, kind="product", use=True, value=3),
                         CutRead(index=1, kind="explain", use=True, value=2, cont=2),
                         CutRead(index=2, kind="explain", use=True, value=2, cont=1),
                         CutRead(index=3, kind="spec", use=True, value=3, cont=4),
                         CutRead(index=4, kind="explain", use=True, value=1, cont=3)])
    p3 = plan_page(cs, rd3, Copy(title="주걱"))
    assert p3.spec == 3 and 4 not in p3.used(), f"짝이 사양으로 간 제목 컷이 사진 사이에 따로 들어감: {p3.used()}"
    assert p3.gallery == [1, 2], f"이어지는 짝이 순서대로 붙지 않음: {p3.gallery}"

    # 사양 자리는 점수 높은 컷, 2점 이상 짝은 사양 바로 앞 — 제목 컷이 치수표를 밀어내던 우산 26·27번 사례
    rd4 = Reading(goods_no="_demo", by_ai=True, hero=0, order=[3, 4],
                  reads=[CutRead(index=0, kind="product", use=True, value=3),
                         CutRead(index=1, kind="explain", use=True, value=2),
                         CutRead(index=2, kind="explain", use=True, value=2),
                         CutRead(index=3, kind="spec", use=True, value=2, cont=4),
                         CutRead(index=4, kind="spec", use=True, value=3, cont=3)])
    p4 = plan_page(cs, rd4, Copy(title="주걱"))
    assert p4.spec == 4, f"점수 낮은 제목 컷이 사양 자리를 차지함: {p4.spec}"
    assert p4.spec_mate == 3 and 3 not in p4.gallery, f"사양 짝이 사진 사이로 떨어짐: {p4.spec_mate} {p4.gallery}"
    assert p4.used()[-2:] == [3, 4], f"사양 짝이 사양 바로 앞에 오지 않음: {p4.used()}"

    # 생성 문구 사후 거르기 — 과장 표현 제거, 목록으로 온 사진 번호 받아주기(2026-09 실측)
    cp = _copy_from_json({"title": "크리어 3단 자동우산", "lead": "최고의 우산",
                          "keys": [["UV차단", "완벽한 자외선 차단"]],
                          "points": [["강한 햇빛도 완벽 차단", "비와 햇빛을 막는다."]],
                          "point_cuts": [[0, 6, 10], 6, "x"]})
    joined = " ".join([cp.title, cp.lead] + [x for k in cp.keys + cp.points for x in k])
    assert "완벽" not in joined and "최고" not in joined, f"과장 표현이 문구에 남음: {joined}"
    assert "한 자외선" not in joined, f"과장 표현을 지우다 조각이 남음: {cp.keys}"
    assert cp.points[0][0] == "강한 햇빛도 차단", cp.points
    assert cp.point_cuts == [0, 6], f"사진 번호 목록을 못 받음: {cp.point_cuts}"
    print("layout._demo self-check OK")




# ══════════════════════════════════════════════════════════════════════
# 블록 편집 — 상세페이지를 "블록 목록"으로 다룬다 (2026-09)
#
# 구간이 코드에 고정돼 있으면 순서를 바꾸거나 중간에 제목·강조를 끼워 넣을 수 없다.
# 그래서 페이지를 블록 목록이라는 데이터로 만들고, 사람이 그 목록을 손보게 한다.
# 초안은 판독 결과로 자동 생성하고, 사람이 고친 목록은 blocks.json에 저장된다.
# ══════════════════════════════════════════════════════════════════════

BLOCK_KINDS = {
    "hero":      "첫 화면 (사진 + 제목)",
    "lead":      "큰 문단",
    "title":     "구간 제목",
    "highlight": "강조 박스",
    "point":     "장점 (번호 + 제목 + 본문 + 사진)",
    "image":     "사진 한 장",
    "keys":      "핵심 요약 띠",
    "usecase":   "이럴 때 씁니다",
    "choice":    "색상·옵션 고르기",
    "spec":      "크기와 사양",
    "policy":    "배송·교환 안내",
}


@dataclass
class Block:
    kind: str
    title: str = ""
    body: str = ""
    cut: int = -1                                          # 사진 컷 번호, -1이면 없음
    items: List[List[str]] = field(default_factory=list)   # 목록형 블록의 내용
    tone: str = "light"                                    # light | dark | deep


def _blocks_path(goods_no: str):
    from .cuts import CUTS_DIR
    return CUTS_DIR / str(goods_no) / "blocks.json"


def save_blocks(goods_no: str, blocks: List[Block]) -> None:
    from dataclasses import asdict
    p = _blocks_path(goods_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(b) for b in blocks], ensure_ascii=False), encoding="utf-8")


def load_blocks(goods_no: str) -> Optional[List[Block]]:
    p = _blocks_path(goods_no)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        out = []
        for d in raw:
            if d.get("kind") in BLOCK_KINDS:
                out.append(Block(kind=d["kind"], title=d.get("title", ""), body=d.get("body", ""),
                                 cut=int(d.get("cut", -1)),
                                 items=[list(x) for x in d.get("items", [])],
                                 tone=d.get("tone", "light")))
        return out or None
    except Exception:
        return None


def draft_blocks(cs: CutSet, reading: Reading, cp: Copy) -> List[Block]:
    """초안 — 판독 결과와 문구로 블록 목록을 짠다. 재료가 없는 블록은 아예 안 만든다."""
    plan = plan_page(cs, reading, cp)
    bs: List[Block] = []
    if plan.hero is not None:
        bs.append(Block("hero", cp.title, cp.lead, plan.hero))
    if cp.empathy.strip():
        bs.append(Block("lead", cp.empathy))
    if cp.keys:
        bs.append(Block("keys", items=[list(k) for k in cp.keys], tone="deep"))
    for n, (t, b) in enumerate(cp.points):
        ci = plan.points[n] if n < len(plan.points) and plan.points[n] is not None else -1
        bs.append(Block("point", t, b, ci, tone="dark" if n % 2 == 0 else "light"))
    if cp.usecase:
        bs.append(Block("usecase", items=[[u] for u in cp.usecase]))
    if plan.swatches:
        bs.append(Block("choice", items=[[str(i), n] for i, n in plan.swatches]))
    for i in plan.gallery:
        bs.append(Block("image", cut=i))
    if plan.spec_mate is not None:
        bs.append(Block("image", cut=plan.spec_mate))
    bs.append(Block("spec", cut=plan.spec if plan.spec is not None else -1, tone="deep"))
    bs.append(Block("policy"))
    return bs


def blocks_used_cuts(blocks: List[Block]) -> List[int]:
    """블록이 쓰는 컷을 화면 순서대로. 맨 앞이 대표이미지가 된다."""
    out = []
    for b in blocks:
        if b.kind == "choice":
            for row in b.items:
                try:
                    i = int(row[0])
                except (ValueError, IndexError):
                    continue
                if i >= 0 and i not in out:
                    out.append(i)
        elif b.cut is not None and b.cut >= 0 and b.cut not in out:
            out.append(b.cut)
    return out


# ── 블록 렌더링 ─────────────────────────────────────────────────────────
def _tone_colors(tone: str, P: dict):
    if tone == "dark":
        return P["base"], "#fff", "rgba(255,255,255,.85)"
    if tone == "deep":
        return P["deep"], "#fff", "rgba(255,255,255,.72)"
    return P["paper"], P["ink"], "rgba(0,0,0,.58)"


def _b_title(b: Block, P: dict) -> str:
    bg, fg, _ = _tone_colors(b.tone, P)
    return (f'<div style="background:{bg};padding:clamp(38px,7vw,68px) clamp(22px,5vw,48px) '
            f'clamp(14px,2.5vw,24px);text-align:center;">'
            f'<div style="font-size:clamp(24px,5vw,38px);font-weight:900;letter-spacing:-.045em;'
            f'line-height:1.25;color:{fg};">{fld(b.title, "title")}</div></div>')


def _b_highlight(b: Block, P: dict) -> str:
    bg, fg, sub = _tone_colors(b.tone if b.tone != "light" else "dark", P)
    body = (f'<div style="font-size:15px;line-height:1.9;color:{sub};max-width:520px;'
            f'margin:12px auto 0;">{fld(b.body, "body")}</div>') if (b.body.strip() or _EDIT) else ""
    return (f'<div style="background:{bg};padding:clamp(40px,7.5vw,72px) clamp(22px,5vw,48px);'
            f'text-align:center;">'
            f'<div style="font-size:clamp(22px,4.6vw,34px);font-weight:900;letter-spacing:-.04em;'
            f'line-height:1.3;color:{fg};">{fld(b.title, "title")}</div>{body}</div>')


def _b_image(b: Block, P: dict, src: Optional[str]) -> str:
    if not src:
        return ""
    cap = (f'<div style="padding:10px clamp(22px,5vw,48px) 18px;font-size:13px;'
           f'color:rgba(0,0,0,.5);text-align:center;">{esc(b.title)}</div>') if b.title.strip() else ""
    return f'<div style="background:{P["paper"]};">{_img(src)}{cap}</div>'


def _b_keys(b: Block, P: dict) -> str:
    rows = [r for r in b.items if (r and r[0].strip())]
    if not rows:
        return ""
    bg, fg, sub = _tone_colors(b.tone if b.tone != "light" else "deep", P)
    cells = "".join(
        f'<div style="flex:1 1 168px;min-width:150px;text-align:center;padding:20px 12px;">'
        f'<div style="font-size:clamp(16px,3.2vw,20px);font-weight:900;color:{fg};'
        f'letter-spacing:-.03em;margin-bottom:6px;">{fld(r[0], f"item:{n}:0")}</div>'
        f'<div style="font-size:13px;color:{sub};line-height:1.55;">'
        f'{fld(r[1] if len(r) > 1 else "", f"item:{n}:1")}</div></div>'
        for n, r in enumerate(rows))
    return (f'<div style="background:{bg};padding:clamp(18px,3.5vw,30px) clamp(14px,3vw,32px);">'
            f'<div style="display:flex;flex-wrap:wrap;max-width:800px;margin:0 auto;">{cells}</div></div>')


def _b_point(b: Block, n: int, P: dict, src: Optional[str]) -> str:
    return _sec_point(n, b.title, b.body, src, P, dark=(b.tone == "dark"))


def _b_usecase(b: Block, P: dict) -> str:
    rows = [r[0] for r in b.items if r and r[0].strip()]
    if not rows:
        return ""
    items = "".join(
        f'<div style="flex:1 1 176px;min-width:150px;border-top:2px solid {P["accent"]};'
        f'padding-top:13px;font-size:15px;font-weight:700;color:{P["ink"]};line-height:1.5;">'
        f'{fld(u, f"item:{n}:0")}</div>' for n, u in enumerate(rows))
    head = fld(b.title, "title") if (b.title.strip() or _EDIT) else "이럴 때 씁니다"
    return (f'<div style="background:{P["soft"]};'
            f'padding:clamp(38px,6.5vw,64px) clamp(22px,5vw,48px);">'
            f'<div style="max-width:820px;margin:0 auto;">'
            f'<div style="font-size:11px;font-weight:800;letter-spacing:.24em;'
            f'color:{P["accent"]};margin-bottom:14px;">{head}</div>'
            f'<div style="display:flex;gap:clamp(14px,3vw,26px);flex-wrap:wrap;">{items}</div></div></div>')


def _b_choice(b: Block, P: dict, url_of) -> str:
    sw = []
    for row in b.items:
        try:
            sw.append((url_of(int(row[0])), row[1] if len(row) > 1 else ""))
        except (ValueError, IndexError):
            continue
    if not sw:
        return ""
    cells = "".join(
        f'<div style="flex:1 1 148px;min-width:130px;max-width:210px;">'
        f'<div style="background:#fff;">{_img(src)}</div>'
        f'<div style="text-align:center;font-size:13px;font-weight:700;color:{P["ink"]};'
        f'margin-top:8px;">{esc(name)}</div></div>' for src, name in sw)
    title = esc(b.title) if b.title.strip() else f"{len(sw)}가지 중에 고르세요"
    return (f'<div style="background:{P["paper"]};padding:clamp(46px,8vw,84px) clamp(22px,5vw,48px);">'
            f'<div style="max-width:820px;margin:0 auto;text-align:center;">'
            f'<div style="font-size:clamp(60px,13vw,110px);font-weight:900;color:{P["accent"]};'
            f'line-height:.9;letter-spacing:-.06em;">{len(sw)}</div>'
            f'<div style="font-size:clamp(23px,4.6vw,35px);font-weight:900;color:{P["ink"]};'
            f'letter-spacing:-.045em;margin:8px 0 30px;">{title}</div>'
            f'<div style="display:flex;gap:clamp(9px,2vw,16px);flex-wrap:wrap;'
            f'justify-content:center;">{cells}</div></div></div>')


def render_blocks(product, cs: CutSet, blocks: List[Block], url_of,
                  editable: bool = False) -> str:
    """블록 목록을 순서대로 이어 붙인다.

    editable=True면 블록마다 래퍼(data-blk)와 편집 칸 표시(data-f)를 넣는다 — 화면에서
    드래그로 순서를 바꾸고 글자를 그 자리에서 고치기 위한 것으로, 실제 등록에 쓰는
    HTML에는 절대 넣지 않는다(구매자 화면에 쓸데없는 속성이 남는다).
    """
    global _EDIT
    _EDIT = editable
    P = page_colors(cs.palette)
    u = lambda i: url_of(i) if (i is not None and i >= 0) else None
    out, pno = [], 0
    for bi, b in enumerate(blocks):
        if b.kind == "hero":
            src = u(b.cut)
            out.append(_sec_hero(src or "", Copy(title=b.title, lead=b.body), P) if src
                       else _b_title(Block("title", b.title, tone="deep"), P))
        elif b.kind == "lead":
            out.append(_sec_empathy(Copy(empathy=b.title or b.body), P))
        elif b.kind == "title":
            out.append(_b_title(b, P))
        elif b.kind == "highlight":
            out.append(_b_highlight(b, P))
        elif b.kind == "image":
            out.append(_b_image(b, P, u(b.cut)))
        elif b.kind == "keys":
            out.append(_b_keys(b, P))
        elif b.kind == "point":
            pno += 1
            out.append(_b_point(b, pno, P, u(b.cut)))
        elif b.kind == "usecase":
            out.append(_b_usecase(b, P))
        elif b.kind == "choice":
            out.append(_b_choice(b, P, url_of))
        elif b.kind == "spec":
            out.append(_sec_spec(product, u(b.cut), P))
        elif b.kind == "policy":
            out.append(_sec_policy(P))
        if editable:
            items_attr = esc("\n".join("|".join(str(x) for x in row) for row in b.items))
            out[-1] = (f'<div data-blk data-idx="{bi}" data-kind="{b.kind}" data-tone="{b.tone}" '
                       f'data-cut="{b.cut}" data-items="{items_attr}" '
                       f'style="position:relative;">{out[-1]}</div>')
    _EDIT = False
    return (f'<div style="font-family:-apple-system,BlinkMacSystemFont,\'Apple SD Gothic Neo\','
            f'\'Malgun Gothic\',sans-serif;max-width:860px;margin:0 auto;background:{P["paper"]};">'
            f'{"".join(out)}</div>')


def _demo_blocks() -> None:
    """블록 편집 자체 점검 — 네트워크·API 호출 없음."""
    from .cuts import Cut
    from .cut_reader import CutRead

    class P:
        name = "테스트 상품"; category = "생활용품>주방용품"; origin_country = "국산"
        manufacturer = "서울산업"; model = "해당없음"; options = []
        supplier = "seoul7rose"; stock = 999999; supply_price = 1200

    cs = CutSet(goods_no="_demo_b", palette=[{"hex": "#cba451", "share": .3, "h": .11, "s": .6, "v": .8}],
                cuts=[Cut(index=i, filename=f"{i:03d}.jpg", width=800, height=800) for i in range(6)])
    rd = Reading(goods_no="_demo_b", by_ai=True, facts=["열탕 소독 가능", "일체형"],
                 reads=[CutRead(index=0, kind="product", use=True),
                        CutRead(index=1, kind="explain", use=True),
                        CutRead(index=2, kind="supplier", use=False, reason="공급사 자료"),
                        CutRead(index=3, kind="spec", use=True),
                        CutRead(index=4, kind="product", use=True, label="블랙"),
                        CutRead(index=5, kind="product", use=True, label="네이비")])
    cp = Copy(title="테스트", lead="배지", empathy="한 줄.\n두 줄.",
              keys=[("열탕", "끓는 물에")], points=[("장점 제목", "본문입니다.")], usecase=["이럴 때"])

    bs = draft_blocks(cs, rd, cp)
    kinds = [b.kind for b in bs]
    assert kinds[0] == "hero", f"첫 블록이 첫 화면이 아님: {kinds}"
    assert kinds[-1] == "policy", "마지막이 배송 안내가 아님"
    assert "choice" in kinds, "라벨 붙은 컷이 있는데 선택지 블록이 없음"

    # (a) 공급사 자료 컷은 어느 블록에도 안 들어간다
    assert 2 not in blocks_used_cuts(bs), "공급사 자료 컷이 블록에 배정됨"

    # (b) 순서를 바꾸고 제목·강조를 끼워 넣어도 렌더링된다
    bs2 = [bs[0], Block("title", "여기서부터 장점"), Block("highlight", "강조할 말", "덧붙이는 설명")] + bs[1:]
    html_out = render_blocks(P(), cs, bs2, lambda i: f"/img/{i}.jpg")
    assert "여기서부터 장점" in html_out and "강조할 말" in html_out, "끼워 넣은 블록이 안 나옴"

    # (c) 잘림을 만드는 스타일이 없어야 한다
    assert "object-fit" not in html_out and "padding-bottom:" not in html_out, "이미지를 자르는 스타일이 남음"

    # (d) 공급사명·도매 재고·매입가는 본문에 안 나간다
    for leak in ("seoul7rose", "999999", "1200"):
        assert leak not in html_out, f"노출되면 안 되는 값: {leak}"

    # (e) 저장·복원이 왕복한다
    save_blocks("_demo_b", bs2)
    back = load_blocks("_demo_b")
    assert back and [b.kind for b in back] == [b.kind for b in bs2], "블록 저장/복원이 어긋남"
    assert back[1].title == "여기서부터 장점"

    # (f) 블록을 다 지워도 터지지 않는다
    assert len(render_blocks(P(), cs, [], lambda i: "")) > 50

    # (g) 대표이미지는 첫 화면 컷과 같다
    used = blocks_used_cuts(bs2)
    assert used and used[0] == bs2[0].cut, "대표이미지가 첫 화면 사진과 다름"

    # (h) 편집 모드 표시가 실제 등록 HTML에 새어나가면 안 된다
    plain = render_blocks(P(), cs, bs2, lambda i: f"/img/{i}.jpg", editable=False)
    for mark in ("data-blk", "data-f=", "data-items"):
        assert mark not in plain, f"편집용 표시가 등록 HTML에 남음: {mark}"
    edit = render_blocks(P(), cs, bs2, lambda i: f"/img/{i}.jpg", editable=True)
    assert edit.count("data-blk") == len(bs2), "편집 모드에서 블록 래퍼 수가 안 맞음"
    assert 'data-f="title"' in edit, "편집 모드인데 제목 칸 표시가 없음"
    # 편집 모드를 한 번 켠 뒤에도 다음 렌더가 깨끗해야 한다(플래그가 남으면 안 됨)
    again = render_blocks(P(), cs, bs2, lambda i: f"/img/{i}.jpg")
    assert "data-f=" not in again, "편집 모드 플래그가 다음 렌더까지 남음"

    import shutil
    from .cuts import CUTS_DIR
    shutil.rmtree(CUTS_DIR / "_demo_b", ignore_errors=True)
    print("layout._demo_blocks self-check OK")


if __name__ == "__main__":
    _demo()
    _demo_blocks()
