"""
도매매 → 스마트스토어 자동 등록 파이프라인.

흐름:
  1. 도매매 키워드 검색 or 상품번호 직접 조회
  2. 상품 상세 정보 + 이미지 취득
  3. 마진 계산 → 판매가 결정 (목표 마진율 20%)
  4. AI 상품명 + 상세설명 생성
  5. 커머스 API 토큰 발급
  6. 스마트스토어 상품 등록 (SUSPENSION 상태)
  7. 결과 저장

CLI:
  python3 main.py register --keyword 실리콘주걱
  python3 main.py register --supply-id 12345678
  python3 main.py register --from-sourcing --dry-run
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from ..margin.calculator import calculate as calc_margin, estimate_sale_price
from ..sourcing.domemae import DomemaeProduct, fetch_product_detail, search_products
from ..sourcing.analyzer import load_from_json
from .auth import get_access_token
from .category import get_category_id
from .content import generate_product_content
from .models import StoreProduct
from .notice import CS_PHONE_NUMBER, DUMMY_CS_PHONE_NUMBER
from .register import build_request_body, register_product

_TARGET_MARGIN = float(os.environ.get("TARGET_MARGIN", "0.20"))
_MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "0.15"))

from ..config import MAX_LISTING_STOCK, MIN_ABS_PROFIT


def _build_cut_detail(product, token: str, dry_run: bool, cut_url=None):
    """저장된 AI 버전 상세페이지(컷 기반)를 그린다. 반환 (html, 이미지URL목록) 또는 None.

    AI 버전은 사람이 미리보기의 "AI로 만들기" 버튼으로 만든 경우에만 쓴다(2026-09).
    예전엔 여기서 초안을 직접 만들어 저장했기 때문에, 미리보기를 열거나 일괄 등록을
    누르기만 해도 사람이 본 적 없는 규칙 기반 페이지가 등록본이 됐다. 저장된 버전이
    없으면 None — 호출부가 도매 원본으로 등록한다.

    이미지 목록의 맨 앞이 대표이미지가 된다 — 페이지 첫 화면에 쓰는 컷과 같은 것이라
    목록 화면과 상세페이지가 어긋나지 않는다.

    등록(dry_run=False)에서는 쓸 컷만 네이버로 올리고 그 주소로 HTML을 짠다. 로컬 파일
    주소가 detailContent에 그대로 들어가면 라이브 페이지의 사진이 전부 깨지므로,
    업로드에 실패한 컷은 plan에서 빼고 그 구간을 비운다.
    """
    from .cuts import load_cuts
    from .layout import blocks_used_cuts, load_blocks, render_blocks

    try:
        blocks = load_blocks(product.goods_no) if product.goods_no else None
        if not blocks:
            return None
        cs = load_cuts(product.goods_no)
        if not cs or not cs.cuts:
            return None
        needed = blocks_used_cuts(blocks)
        if not needed:
            return None
        by_index = {c.index: c for c in cs.cuts}

        if dry_run:
            # 미리보기 — 로컬에서만 보이는 주소. 등록(dry_run=False)은 이 분기를 타지 않는다.
            # cut_url이 없는 CLI dry-run에서는 파일 경로를 넣어 어떤 컷이 쓰였는지 보이게 한다.
            url_of = ((lambda i: cut_url(product.goods_no, by_index[i].filename)) if cut_url
                      else (lambda i: str(cs.path_of(by_index[i]))))
            return render_blocks(product, cs, blocks, url_of), []

        from .images import upload_images
        uploaded = upload_images([str(cs.path_of(by_index[i])) for i in needed if i in by_index], token)
        ok = {i: u for i, u in zip([i for i in needed if i in by_index], uploaded) if u}
        if not ok:
            return None
        if len(ok) < len(needed):
            print(f"  [경고] 컷 {len(needed) - len(ok)}장을 못 올려 그 자리를 비웁니다")
        # 못 올린 컷은 블록에서 사진만 뺀다 — 글은 그대로 남는다.
        blocks = [b for b in blocks
                  if not (b.kind == "image" and b.cut not in ok)]
        for b in blocks:
            if b.kind != "choice" and b.cut is not None and b.cut >= 0 and b.cut not in ok:
                b.cut = -1
            if b.kind == "choice":
                b.items = [r for r in b.items if str(r[0]).isdigit() and int(r[0]) in ok]
        html = render_blocks(product, cs, blocks, lambda i: ok.get(i, ""))
        return html, [ok[i] for i in needed if i in ok]
    except Exception as e:
        print(f"  [경고] 컷 기반 상세페이지 생성 실패 — 기존 방식으로 대체합니다 ({e})")
        return None


_CUT_URL_RE = re.compile(r'["\']([^"\']*/cut/([^/"\']+)/([^/"\']+\.(?:jpg|jpeg|png)))["\']', re.I)


def _swap_cut_urls(html: str, token: str):
    """미리보기에서 만든 로컬 컷 주소를 네이버 주소로 바꾼다. 반환 (html, 올라간URL목록).

    이걸 안 하면 상세페이지에 `/cut/...` 같은 우리 컴퓨터 주소가 그대로 들어가 라이브
    페이지의 사진이 전부 깨진다. 사람이 미리보기에서 한 글자라도 고치면 그 HTML이
    통째로 등록에 실려가기 때문에 반드시 여기를 거쳐야 한다(2026-09).

    올리지 못한 컷은 주소를 남기지 않고 <img> 태그째 뺀다 — 깨진 사진 자리를 남기는
    것보다 낫고, 우리 로컬 주소가 구매자 화면에 노출되지도 않는다.
    """
    from .cuts import CUTS_DIR

    found = []
    for m in _CUT_URL_RE.finditer(html):
        if all(m.group(1) != f[0] for f in found):
            found.append((m.group(1), m.group(2), m.group(3)))
    if not found:
        return html, []

    from .images import upload_images
    uploaded = upload_images([str(CUTS_DIR / g / f) for _, g, f in found], token)
    ok = []
    for (orig, _g, _f), up in zip(found, uploaded):
        if up:
            html = html.replace(orig, up)
            ok.append(up)
        else:
            html = re.sub(r'<img[^>]+src=["\']' + re.escape(orig) + r'["\'][^>]*/?>', "", html)
    if len(ok) < len(found):
        print(f"  [경고] 컷 {len(found) - len(ok)}장을 못 올려 상세페이지에서 뺐습니다")
    return html, ok


def run(
    keyword: str = "",
    supply_id: str = "",
    from_sourcing: bool = False,
    sourcing_log: Optional[Path] = None,
    dry_run: bool = False,
    status: str = "SUSPENSION",
    output_path: Optional[Path] = None,
    name_override: str = "",
    tags_override: Optional[List[str]] = None,
    sale_price_override: Optional[int] = None,
    detail_override: str = "",
    discount_rate: float = 0.0,
    representative_image_override: str = "",
    field_overrides: Optional[dict] = None,
    force: bool = False,
    cut_url=None,
    skip_cuts: bool = False,
) -> List[StoreProduct]:
    """
    전체 자동 등록 파이프라인.

    Args:
        keyword      : 키워드로 도매매 검색
        supply_id    : 도매매 상품번호 직접 지정 — 지정하면 검색을 건너뛰고 정확히 이 상품만
                       처리하므로, 미리보기에서 확인한 상품과 실제 등록 상품이 어긋나지 않음
                       (키워드 검색은 재고/가격 변동에 따라 다른 최저가 상품이 뽑힐 수 있음)
        from_sourcing: sourcing_log.json에서 "진입 권장" 상품 자동 처리
        sourcing_log : sourcing_log.json 경로
        dry_run      : True면 실제 등록 안 하고 요청 바디만 출력
        status       : 등록 상태 (SUSPENSION / ON)
        output_path  : 등록 결과 저장 경로
        name_override: 지정하면 AI/자동생성 상품명 대신 이 값을 그대로 사용 (미리보기에서
                       사용자가 수정한 이름을 반영할 때 사용)
        tags_override: 지정하면 자동생성 검색어 태그 대신 이 목록을 그대로 사용 (미리보기에서
                       추천 키워드를 골라 담은 값을 반영할 때 사용)
        sale_price_override: 지정하면 자동계산 판매가 대신 이 값을 그대로 사용. 단 최소
                       마진율/절대이익 게이트는 그대로 적용되므로, 마진이 안 나오는 가격을
                       넣으면 다른 사유들과 마찬가지로 [건너뜀] 처리된다(2026-09)
        detail_override: 지정하면 자동생성 상세설명 HTML 대신 이 값을 그대로 사용. 리메이크
                       (트랙B)처럼 상세페이지를 직접 손봐야 하는 경우 미리보기에서 편집한
                       내용을 등록에 반영할 때 쓴다(2026-09)
        discount_rate: 즉시할인율(0~1). sale_price는 항상 할인 전 정가이고, 실제 받는 돈은
                       sale_price*(1-discount_rate)이므로 마진 게이트를 할인 후 가격 기준으로
                       한 번 더 확인한다 — 할인이 마진을 깎아 절대이익 미달로 만들 수 있다(2026-09)
        representative_image_override: 지정하면 도매매 원본 대표이미지 대신 이 URL을
                       사용(AI로 새로 만든 이미지 등록용, 2026-09). 로컬에서 접근 가능한
                       URL이어야 한다 — 등록 시 이 URL을 다운로드해 네이버 서버로 올린다.
        field_overrides: 등록 항목 점검 패널에서 문제로 잡힌 항목(더미 A/S 번호·빈 제조사 등)을
                       직접 고친 값. key는 "detailAttribute.brandName" 같은 점(.) 경로 —
                       build_request_body()가 최종 바디에 덮어쓴다(2026-09).
        force        : True면 부실 리스팅 경고(사진 1장/설명 부족/저해상도)가 있어도 등록 강행.
                       기본값은 False로, 해당 조건이면 자동으로 건너뜀

    Returns:
        등록 완료된 StoreProduct 리스트
    """
    domemae_products: List[DomemaeProduct] = []

    # ── Step 1: 도매매 상품 수집 ───────────────────────────────────────────
    if supply_id:
        print(f"\n[1] 도매매 상품 상세 조회: {supply_id}")
        try:
            p = fetch_product_detail(supply_id)
            domemae_products = [p]
            print(f"  → {p.name} (도매가: {p.supply_price:,}원)")
        except Exception as e:
            print(f"  [오류] 상품 조회 실패: {e}")
            return []

    elif keyword:
        print(f"\n[1] 도매매 키워드 검색: '{keyword}'")
        try:
            result = search_products(keyword, limit=5)
            if not result.products:
                print("  검색 결과 없음")
                return []
            # 최저가 상품 선택
            domemae_products = [result.cheapest or result.products[0]]
            p = domemae_products[0]
            print(f"  → {p.name} (도매가: {p.supply_price:,}원, {result.total}개 결과 중 최저가)")

            # 상세 정보 재조회 (이미지 포함)
            if p.goods_no:
                try:
                    detailed = fetch_product_detail(p.goods_no)
                    domemae_products = [detailed]
                except Exception:
                    pass  # 상세 조회 실패 시 검색 결과 그대로 사용
        except Exception as e:
            print(f"  [오류] 도매매 검색 실패: {e}")
            return []

    elif from_sourcing:
        print("\n[1] 소싱 로그에서 진입 권장 상품 로드")
        log_path = sourcing_log or Path("data/sourcing_log.json")
        candidates = load_from_json(log_path)
        # discover.py의 "진입 권장" 기준(55점)과 통일 — 예전엔 70점이었는데
        # discover.py 점수 체계 재설계(2026-07-30) 이후로 안 맞춰져 있었다.
        # 화면엔 "진입 권장"이라고 뜨는 상품이 자동등록만 안 되는 불일치였다 (2026-08).
        # 트랙B(리메이크)는 점수만으로 걸러지지 않는다 — "리메이크 권장"이 상세페이지를
        # 새로 만들어야 이길 여지가 있다는 뜻인데, 자동등록은 공급사 원본을 그대로 쓰므로
        # 트랙B를 자동등록하면 리메이크 없이 원본 그대로 나간다(2026-09 발견). 트랙A만 대상.
        eligible = [c for c in candidates if c.score >= 55 and c.track == "A"]
        # 오매칭 차단 — 실물 미확인이거나 자동판정이 불일치를 의심하면 자동등록에서도
        # 제외한다. webapp의 등록 경로(register_candidate/register_candidates_bulk)와
        # 같은 게이트를 여기도 태운다(2026-09).
        from ..sourcing.models import registration_block_reason
        recommended = [c for c in eligible if not registration_block_reason(c.supply_matched, c.human_confirmed)]
        blocked_count = len(eligible) - len(recommended)
        if blocked_count:
            print(f"  → {blocked_count}개는 실물확인 미완료/오매칭 의심으로 제외")
        if not recommended:
            print("  진입 권장(트랙A, 55점 이상) 상품 없음")
            return []
        print(f"  → {len(recommended)}개 상품 처리 예정")
        # 각 키워드별로 파이프라인 실행
        results = []
        for c in recommended:
            print(f"\n{'─'*50}")
            sub = run(
                keyword=c.keyword,
                dry_run=dry_run,
                status=status,
                output_path=output_path,
                force=force,
            )
            results.extend(sub)
            time.sleep(1.0)
        return results

    else:
        print("[오류] --keyword, --supply-id, --from-sourcing 중 하나를 지정하세요.")
        return []

    registered = []
    already_registered = _load_registered_goods_nos(output_path, get_access_token())

    for domemae_p in domemae_products:
        if domemae_p.goods_no and domemae_p.goods_no in already_registered:
            print(f"\n[건너뜀] 도매매 {domemae_p.goods_no}는 이미 등록된 상품 — 중복 등록 방지")
            continue

        # KC 인증(전기용품·어린이제품) 대상 필터 — discover.py는 "검색 키워드"에만 이
        # 목록을 적용해서, 키워드는 깨끗한데 실제 매칭된 도매매 상품이 전동/아동용이면
        # 그대로 등록되고 있었다. --supply-id 직접 지정 경로는 키워드 필터 자체를 아예
        # 안 거치므로 여기(실제 등록되는 도매매 상품명 기준)서 한 번 더, 모든 경로
        # 공통으로 막는다(2026-09) — 인증서류 확보가 안 되는 위탁 소싱 특성상 원천 제외.
        from ..config import BLOCKED_ELECTRIC_KEYWORDS, BLOCKED_KIDS_KEYWORDS
        kc_hit = next(
            (w for w in BLOCKED_ELECTRIC_KEYWORDS + BLOCKED_KIDS_KEYWORDS if w in domemae_p.name),
            "",
        )
        if kc_hit:
            print(f"  [건너뜀] KC 인증 대상 의심 — 상품명에 '{kc_hit}' 포함 (도매매: {domemae_p.name[:40]})")
            continue

        kw = keyword or domemae_p.name.split()[0]

        # ── Step 2: 마진 계산 → 판매가 결정 ──────────────────────────────
        print(f"\n[2] 마진 계산 (도매가: {domemae_p.supply_price:,}원)")
        sale_price = sale_price_override if sale_price_override else _decide_sale_price(
            domemae_p.supply_price, domemae_p.retail_price
        )
        margin = calc_margin(
            sale_price=sale_price,
            cost_price=domemae_p.supply_price,
            free_shipping=(sale_price >= 30_000),
        )
        margin_flag = "✓" if margin.passes_target else ("△" if margin.passes_min else "✗")
        print(f"  판매가: {sale_price:,}원  마진율: {margin.margin_rate:.1%} {margin_flag}")

        # _decide_sale_price()가 정상 경로에선 이미 마진율·절대이익 둘 다 만족하는 값을
        # 돌려주지만(estimate_sale_price가 둘 중 높은 쪽으로 역산), 발굴 단계(discover.py)는
        # 두 조건을 독립적으로 다시 확인한다 — 여기도 같은 안전장치를 둔다. name_override처럼
        # 앞으로 판매가에 사람이 개입할 여지가 생기면 이 지점이 마지막 방어선이 된다
        # (실제로 절대이익 660원/943원짜리가 등록됐던 사고가 이 게이트 부재로 발생했다, 2026-09).
        if not margin.passes_min:
            print(f"  [건너뜀] 최소 마진율({_MIN_MARGIN:.0%}) 미달")
            continue
        if not margin.passes_abs_floor:
            print(f"  [건너뜀] 절대이익 {margin.net_profit:,}원 — 기준({MIN_ABS_PROFIT:,}원) 미달")
            continue

        # 즉시할인이 있으면 구매자가 실제로 내는 돈은 sale_price가 아니라 할인된 가격이다 —
        # 위 게이트는 정가 기준으로만 통과했을 뿐, 할인 후에도 마진이 남는지는 따로 봐야
        # 한다. 확인 안 하면 "정가는 마진 남는데 할인가는 역마진"인 상품이 그대로 나간다.
        if discount_rate:
            discounted_price = round(sale_price * (1 - discount_rate))
            discounted_margin = calc_margin(
                sale_price=discounted_price,
                cost_price=domemae_p.supply_price,
                free_shipping=(discounted_price >= 30_000),
            )
            if not discounted_margin.passes_min or not discounted_margin.passes_abs_floor:
                print(
                    f"  [건너뜀] 할인 적용가 {discounted_price:,}원 기준 마진 미달 "
                    f"(할인율 {discount_rate:.0%}, 절대이익 {discounted_margin.net_profit:,}원)"
                )
                continue

        # 원산지 — 네이버 코드표에서 실제 코드를 찾을 수 있어야 등록한다.
        # 예전엔 "국내산인지"만 검사하고 정작 코드는 03(=상세설명에 표시)을 박아넣고 있었다
        # (2026-08-10 발견 — 주석엔 03이 국산이라고 적혀 있었으나 실제 03은 다른 값).
        # 이제 도매매 원산지("수입산_아시아_중국")를 코드로 변환하고, 못 찾으면 등록을 막는다.
        from .origin import resolve_origin_code
        origin_code = resolve_origin_code(domemae_p.origin_country, get_access_token())
        if not origin_code:
            print(
                f"  [건너뜀] 원산지 '{domemae_p.origin_country or '(미표기)'}' — "
                "네이버 원산지 코드표에서 찾지 못함, 잘못된 원산지 표시를 막기 위해 등록 금지"
            )
            continue

        # A/S 연락처 — register.py의 build_request_body()가 이 값이 더미면 등록을 막지만,
        # 그 체크는 이미지 업로드(Step 3.5) 뒤에 걸려 있어 시도할 때마다 네이버 서버에
        # 못 쓰는 이미지만 쌓이고 있었다. 원산지 게이트와 같은 자리(이미지 업로드 전)로
        # 옮겨서 같은 실패를 업로드 전에 잡는다(2026-09).
        if CS_PHONE_NUMBER == DUMMY_CS_PHONE_NUMBER:
            print("  [건너뜀] A/S 연락처 미설정 — .env의 CS_PHONE_NUMBER를 실제 번호로 채워야 등록 가능")
            continue

        # 리스팅 품질 체크 — 사진 1장뿐이거나 설명이 짧으면 최저가만 보고 고른
        # 부실한 리스팅일 가능성이 높음 (2026-07-12: 실전 등록 테스트로 발견된 패턴).
        # 기본은 자동 건너뜀 — --force로만 강행 등록 가능.
        quality_issues = []
        if len(domemae_p.images) <= 1:
            quality_issues.append(f"이미지가 {len(domemae_p.images)}장뿐")
        if len(domemae_p.description) < 200:
            quality_issues.append(f"상세설명이 {len(domemae_p.description)}자로 짧음")
        if domemae_p.images:
            from .images import check_min_resolution
            size = check_min_resolution(domemae_p.images[0])
            if size and min(size) < 1000:
                quality_issues.append(f"대표이미지 해상도 {size[0]}x{size[1]}px (권장 최소 1000px 미만)")

        if quality_issues:
            label = "[경고]" if force else "[건너뜀]"
            print(f"  {label} 부실 리스팅 의심 — {', '.join(quality_issues)}")
            if not force:
                print("  강행하려면 --force 옵션 사용")
                continue

        # ── Step 3: 카테고리 결정 (실제 커머스 API 카테고리 트리 기반 검색) ──
        token = get_access_token()
        cat_id = get_category_id(kw, domemae_p.category, token)
        if not cat_id:
            print(f"\n[3] [건너뜀] 카테고리 자동 매칭 실패 (키워드: '{kw}', 도매매 카테고리: '{domemae_p.category}') — 잘못된 카테고리로 등록되는 걸 방지하기 위해 건너뜀. 수동으로 카테고리 지정 필요")
            continue
        from .category import describe_category
        category_name = describe_category(cat_id, token)
        print(f"\n[3] 카테고리 ID: {cat_id} ({category_name})")

        # 카테고리 속성(색상/소재/사이즈 등) — 네이버쇼핑 SEO 가이드가 "필터 결과 최상단
        # 노출"의 조건으로 명시하는데 지금까지 아예 안 보내고 있었다(2026-09 발견).
        # 조회 실패해도 등록을 막지 않는다 — attributes.py 참고.
        from .attributes import fetch_category_attributes, match_attributes
        attribute_specs = fetch_category_attributes(cat_id, token)
        matched_attributes = match_attributes(
            attribute_specs, domemae_p.name, domemae_p.option_group_name, domemae_p.options,
        ) if attribute_specs else []
        if matched_attributes:
            print(f"  속성 {len(matched_attributes)}개 매칭")

        # ── Step 3.5: 이미지를 네이버 서버로 옮기기 ─────────────────────────
        # 상세설명 HTML 안의 사진이 도매매 CDN 주소를 그대로 가리키고 있었다
        # (2026-08-10 등록 상품 조회로 확인 — cdn1.domeggook.com 3장). 공급사가 상품을
        # 내리면 우리 상세페이지 사진이 통째로 사라진다. 대표/추가 이미지만 옮기고
        # 상세설명은 놔뒀던 게 원인이라, 콘텐츠를 만들기 **전에** 전부 옮기고 주소를
        # 바꿔치기한 뒤 그 주소로 상세설명을 만든다.
        # dry-run에서는 실제 업로드를 하지 않으므로 미리보기엔 도매매 주소가 그대로 보인다.
        # AI로 새로 만든 대표이미지도 네이버는 자기 서버에 올라간 이미지만 받으므로
        # (외부 URL은 InvalidImageUrl), 도매매 원본과 똑같이 이 업로드 단계를 거쳐야 한다 —
        # 그래서 등록 직전에 갈아치우지 않고 목록 맨 앞에 끼워 넣어 같은 파이프를 태운다(2026-09).
        if representative_image_override:
            domemae_p.images = [representative_image_override] + [
                u for u in domemae_p.images if u != representative_image_override
            ]

        # 사람이 만든 AI 버전(컷 기반 상세페이지)이 저장돼 있으면 그걸 쓴다. 그러면 쓸 컷만
        # 네이버로 올라가므로 아래의 원본 일괄 업로드 경로를 타지 않는다. 저장된 버전이
        # 없으면 도매 원본 그대로 — 여기서 초안을 새로 만들지 않는다(2026-09).
        cut_detail, cut_images = "", []
        if not detail_override and not skip_cuts:
            built = _build_cut_detail(domemae_p, token, dry_run, cut_url=cut_url)
            if built:
                cut_detail, cut_images = built
                if cut_images:
                    print(f"\n[3.5] 컷 {len(cut_images)}장으로 상세페이지 재구성")

        url_map = {}
        if not dry_run and not cut_images:
            from .images import upload_images
            originals = [u for u in domemae_p.images if u][:10]
            # upload_images()는 원본과 같은 길이로 반환하고 실패분을 None으로 채운다
            # (2026-09 수정 전엔 실패분을 건너뛴 짧은 리스트를 반환해 dict(zip(...))가
            # 엉뚱한 원본-업로드 URL을 짝짓고 있었다 — 5장 중 2장 실패 시 대표이미지까지
            # 바뀌는 사고로 실증됨).
            uploaded = upload_images(originals, token)
            url_map = {orig: up for orig, up in zip(originals, uploaded) if up}
            if not url_map:
                print("  [건너뜀] 이미지 업로드 실패 — 등록 가능한 이미지 없음")
                continue
            # 업로드 상한(10장)을 넘거나 다운로드에 실패한 이미지는 공급사(도매매) 원본
            # 주소로 남겨두지 않고 통째로 뺀다 — 남겨두면 소싱처가 노출되고 공급사가
            # 사진을 지우면 상세페이지가 깨진다(2026-09 실증: 17장 중 10장만 업로드돼
            # 7장이 공급사 도메인 주소로 라이브에 남아있었음).
            leaked = [u for u in domemae_p.images if u and u not in url_map]
            domemae_p.images = [url_map[u] for u in domemae_p.images if u in url_map]
            for old, new in url_map.items():
                domemae_p.description = domemae_p.description.replace(old, new)
            for leaked_url in leaked:
                domemae_p.description = re.sub(
                    r'<img[^>]+src=["\']' + re.escape(leaked_url) + r'["\'][^>]*/?>',
                    "", domemae_p.description,
                )
            print(f"\n[3.5] 이미지 {len(url_map)}장을 네이버 서버로 업로드"
                  + (f" ({len(leaked)}장은 업로드 못 해 상세페이지에서 제외)" if leaked else ""))

        # ── Step 4: AI 콘텐츠 생성 ────────────────────────────────────────
        print(f"\n[4] 상품 콘텐츠 생성 중...")
        content = generate_product_content(kw, domemae_p, sale_price, category_name=category_name)
        if name_override:
            content["name"] = name_override
        if tags_override is not None:
            content["tags"] = tags_override
        if detail_override:
            # 미리보기에서 편집한 HTML을 그대로 쓴다 — 단, 미리보기는 dry-run이라 이미지가
            # 아직 도매매 CDN 주소일 수 있다. url_map(위 Step 3.5)으로 같은 치환을 한 번 더
            # 해줘야, 편집한 내용에 남아있는 도매매 주소도 네이버 주소로 바뀐다(2026-09).
            for old_url, new_url in url_map.items():
                detail_override = detail_override.replace(old_url, new_url)
            # 미리보기에서 만든 컷 주소는 우리 컴퓨터에서만 열린다 — 네이버로 올리고
            # 주소를 바꿔 끼우지 않으면 라이브 상세페이지 사진이 전부 깨진다.
            if not dry_run:
                detail_override, swapped = _swap_cut_urls(detail_override, token)
                if swapped and not cut_images:
                    # 상세페이지에 쓴 컷을 대표·추가 이미지로도 쓴다. (Step 3.5에서 올린
                    # 도매 원본은 이 경우 안 쓰이지만, 컷이 하나도 안 올라갔을 때를 위한
                    # 보험이라 그대로 둔다.)
                    cut_images = swapped
            content["detail_content"] = detail_override
        elif cut_detail:
            content["detail_content"] = cut_detail
        print(f"  상품명: {content['name']}")

        # ── Step 5: StoreProduct 구성 ─────────────────────────────────────
        store_product = StoreProduct(
            name=content["name"],
            leaf_category_id=cat_id,
            sale_price=sale_price,
            stock_quantity=min(domemae_p.stock, MAX_LISTING_STOCK),
            detail_content=content["detail_content"],
            # 컷으로 재구성했으면 상세페이지 첫 화면에 쓴 컷이 그대로 대표이미지가 된다 —
            # 목록에서 본 사진과 페이지 첫 장면이 어긋나지 않는다.
            representative_image=(cut_images[0] if cut_images else domemae_p.main_image),
            # 네이버는 대표 1장 + 추가 9장까지 받는데 3장만 올리고 있었다(2026-09).
            optional_images=(cut_images[1:10] if cut_images else domemae_p.images[1:10]),
            supply_price=domemae_p.supply_price,
            margin_rate=margin.margin_rate,
            domemae_goods_no=domemae_p.goods_no,
            domemae_category=domemae_p.category,
            supplier=domemae_p.supplier,
            keyword=kw,
            tags=content.get("tags", []),
            origin_country=domemae_p.origin_country,
            origin_code=origin_code,
            manufacturer=domemae_p.manufacturer,
            model=domemae_p.model,
            option_group_name=domemae_p.option_group_name,
            options=domemae_p.options,
            attributes=matched_attributes,
            discount_rate=discount_rate,
            field_overrides=field_overrides or {},
        )

        if dry_run:
            # ── dry-run: 등록 바디 출력 ────────────────────────────────
            # build_request_body는 A/S 연락처가 더미면 ValueError로 막는다(D-3, register.py
            # 참고) — 다른 [건너뜀] 사유들과 같은 방식으로 이 상품만 건너뛰고 계속 진행한다.
            print(f"\n[dry-run] 등록 요청 바디 미리보기:")
            try:
                import json as _json
                body = build_request_body(store_product, status=status, access_token=token)
            except ValueError as e:
                print(f"  [건너뜀] {e}")
                continue
            print(_json.dumps(body, ensure_ascii=False, indent=2)[:1000])
            print(f"\n  {store_product.summary()}")
            registered.append(store_product)
            continue

        # ── Step 6: 상품 등록 ─────────────────────────────────────────────
        # 이미지는 Step 3.5에서 이미 네이버 주소로 바꿔뒀다 (대표/추가/상세설명 전부)
        print(f"\n[5] 스마트스토어 상품 등록 중...")
        try:
            product_id = register_product(store_product, token, status=status)
            store_product.naver_product_id = product_id
            print(f"  등록 완료! 상품 ID: {product_id}  상태: {status}")
        except Exception as e:
            print(f"  [오류] 등록 실패: {e}")
            continue

        registered.append(store_product)

        # ── Step 7: 결과 저장 ─────────────────────────────────────────────
        _save_result(store_product, output_path)

    return registered


def _decide_sale_price(supply_price: int, retail_price: int) -> int:
    """
    판매가 결정 로직.
    목표 마진율 20% 달성 가능한 최소 판매가로 설정.
    소비자가가 있으면 참고하되, 마진율 기준 우선.
    """
    target_price = estimate_sale_price(supply_price)

    # 소비자가가 있고 목표 마진율을 달성하면 소비자가 참고
    if retail_price and retail_price >= target_price:
        # 소비자가보다 5~10% 낮게 시작
        competitor_based = int(retail_price * 0.92)
        if competitor_based >= target_price:
            return competitor_based

    return target_price


def _load_registered_goods_nos(output_path: Optional[Path], access_token: str = "") -> set:
    """이미 등록한 도매매 상품번호 집합 — 중복 등록 방지용.

    로컬 원장(registered_products.json)만 보면 파일이 없거나 깨졌을 때 조용히 빈
    값을 반환해 전 상품을 신규로 간주하는 사고가 있었다(2026-09, 경로도 파이프라인은
    CWD 상대경로/웹앱은 절대경로라 실행 위치에 따라 갈렸음). 네이버 실제 등록 목록
    (register.py가 심어둔 sellerManagementCode=도매매 상품번호)을 우선 조회하고,
    로컬 파일은 합집합으로만 보강한다 — 한쪽이 실패해도 다른 쪽으로 방지가 유지된다.
    """
    live = set()
    if access_token:
        try:
            from .product_status import fetch_product_statuses
            live = {s.seller_management_code for s in fetch_product_statuses(access_token) if s.seller_management_code}
        except Exception as e:
            print(f"  [경고] 네이버 등록 상품목록 조회 실패 — 로컬 원장만으로 중복 등록 방지: {e}")

    path = output_path or Path("data/registered_products.json")
    local = set()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            local = {p.get("domemae_goods_no", "") for p in existing if p.get("domemae_goods_no")}
        except Exception as e:
            print(f"  [경고] 로컬 등록 원장 읽기 실패({path}): {e}")

    return live | local


def _save_result(product: StoreProduct, output_path: Optional[Path]) -> None:
    path = output_path or Path("data/registered_products.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing.append(product.to_dict())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
