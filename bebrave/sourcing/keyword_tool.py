"""
네이버 검색광고 API — 키워드 자동 탐색 + 실제 월간 검색수 조회.

시드 키워드(카테고리명) 입력 → 관련 키워드 자동 수집 → 월간 검색수 반환.
이 데이터로 골든레이시오(실제검색수 ÷ 등록상품수)를 정확하게 계산 가능.

API: GET https://api.naver.com/keywordstool
  - hintKeywords: 시드 키워드 (최대 5개, 쉼표 구분)
  - showDetail=1: 월간 PC+모바일 검색수 포함
  - 반환: 관련 키워드 최대 100개 + 월간 검색수

인증 방식 (HMAC-SHA256):
  X-Timestamp  : Unix ms
  X-API-KEY    : 액세스키
  X-Customer   : 고객ID
  X-Signature  : base64(HMAC-SHA256(timestamp.METHOD.uri, base64decode(비밀키)))
"""
import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from ..config import (
    EXPANSION_DEPTH,
    RESEED_MIN_SEARCH,
    RESEED_MAX_SEARCH,
    RESEED_PER_LEVEL,
)

_BASE_URL = "https://api.searchad.naver.com"

# 네이버 쇼핑 2~3뎁스 기준 시드 키워드
# 제약 있는 하위 카테고리만 제외하고 나머지 전체 포함
# 제외 기준: KC인증 필수(전기제품·아동제품), 식품위생법, 의료기기법, 화장품법
CATEGORY_SEEDS = {
    # ══ 페르소나: 2030 1인 여성 라이프스타일 (2026-07 전면 재구성) ═══════════
    # 카테고리 키는 네이버 실제 2뎁스 카테고리명과 동일하게 맞춤(사용자가 직접
    # 네이버 카테고리 트리에서 선별한 46개). 등록 단계 매칭 손실을 없애기 위함.

    # ── 패션잡화 ─────────────────────────────────────────────────────
    "가방소품":     ["가방정리파우치", "가방속가방", "가방고리참", "가방체인스트랩", "네임택가방"],
    "모자":        ["와이어버킷햇", "코듀로이볼캡", "니트비니", "체크버킷햇", "데님볼캡"],
    "벨트":        ["체인벨트", "가죽벨트여성", "와이드벨트", "링벨트", "리본벨트"],
    "신발용품":     ["신발깔창", "신발탈취제", "신발끈세트", "신발주걱", "구두약세트"],
    "양말":        ["무지외반증양말", "발목양말세트", "골지크루삭스", "타비양말", "레이스양말"],
    "여성가방":     ["미니크로스백", "나일론버킷백", "반달백", "체인숄더백", "미니토트백"],
    "여성신발":     ["메리제인플랫", "발레리나플랫슈즈", "첼시부츠여성", "여성스니커즈", "슬링백구두"],
    "여행용가방/소품": ["기내용캐리어", "여행파우치세트", "압축백여행용", "여권케이스", "캐리어네임태그"],
    "장갑":        ["가죽장갑여성", "니트장갑", "운전장갑여성", "터치장갑", "라이딩장갑"],
    "주얼리":      ["진주목걸이", "체인반지", "이어커프", "실버귀걸이", "레이어드팔찌"],
    "지갑":        ["미니카드지갑", "반지갑여성", "장지갑여성", "동전지갑", "카드홀더"],
    "패션소품":     ["브로치세트", "실크헤어밴드", "선글라스체인", "손수건스카프", "미니벨트백"],
    "헤어액세서리":  ["아크릴집게핀", "빈티지헤어핀", "진주집게핀", "꽃집게핀", "Y2K헤어클립"],

    # ── 패션의류 ─────────────────────────────────────────────────────
    "여성의류":     ["여성린넨원피스", "여성니트가디건", "여성와이드팬츠", "여성크롭탑", "여성트렌치코트"],
    "여성언더웨어/잠옷": ["브라렛세트", "실크잠옷세트", "홈웨어세트", "면속옷세트", "커플잠옷"],

    # ── 화장품/미용 (도구류만) ─────────────────────────────────────────
    "뷰티소품":     ["클렌징패드세트", "화장면봉", "뷰티블렌더세트", "메이크업스펀지", "파우더퍼프"],
    "네일케어":     ["큐티클오일", "네일파일세트", "발톱깎이세트", "각질리무버스틱", "네일버퍼"],
    "헤어스타일링":  ["미니고데기거치대", "헤어롤러세트", "볼륨롤빗", "헤어핀셋", "앞머리클립"],
    "헤어케어":     ["정전기방지빗", "두피스케일러브러쉬", "두피마사지브러쉬", "헤어에센스브러쉬", "실크헤어터번"],

    # ── 생활/건강 ─────────────────────────────────────────────────────
    "수납/정리용품": ["속옷정리함", "냉장고정리용기", "서랍정리트레이", "옷장수납박스", "선반정리대"],
    "문구/사무용품": ["감성마스킹테이프", "다이어리스티커", "투명스티커팩", "떡메모지세트", "아크릴책갈피"],
    "발건강용품":   ["족욕기용소금", "발각질제거기", "발뒤꿈치패치", "발가락교정기", "종아리압박밴드"],
    "좌욕/좌훈용품": ["좌훈의자커버", "좌욕기용소금", "궁중좌훈세트", "좌욕대야", "좌훈방석"],
    "반려동물":     ["고양이낚시대장난감", "강아지노즈워크매트", "캣타워소형", "강아지터그장난감", "고양이터널"],
    "원예/식물":    ["미니화분세트", "수경재배용기", "다육이화분", "식물영양제스틱", "화분받침대"],
    "생활용품":     ["실리콘밀폐용기", "다용도정리바구니", "행거정리대", "다용도수납케이스", "실리콘냄비받침"],
    "세탁용품":     ["세탁볼", "빨래집게세트", "섬유탈취제", "세탁망세트", "울샴푸"],
    "청소용품":     ["극세사걸레", "청소솔세트", "먼지제거롤러", "욕실청소솔", "다용도스퀴지"],
    "건강관리용품":  ["폼롤러소형", "스트레칭밴드", "압박스타킹", "영양제보관함", "요가블록세트"],
    "욕실용품":     ["실리콘칫솔꽂이", "욕실매트세트", "샤워타올세트", "비누받침대", "욕실수납선반"],

    # ── 가구/인테리어 (소형·소품 위주, 대형가구 제외) ────────────────────
    "인테리어소품":  ["미니조명스탠드", "인테리어액자", "드라이플라워", "캔들홀더세트", "미니화분"],
    "홈데코":      ["타피스트리원단", "인테리어가랜드", "벽거울소형", "리스장식", "홈데코오브제"],
    "침구단품":     ["누빔이불커버", "베개커버세트", "매트리스커버", "차렵이불", "극세사이불"],
    "침구세트":     ["구스침구세트", "여름침구세트", "겨울침구세트", "호텔식침구세트", "차렵이불세트"],
    "베개":       ["메모리폼베개", "경추베개", "라텍스베개", "쿨링베개", "목베개커버"],
    "카페트/러그":   ["극세사러그", "패브릭러그소형", "타프팅러그", "현관매트러그", "원형러그"],
    "커튼/블라인드": ["암막커튼", "롤스크린블라인드", "쉬폰커튼", "미니블라인드", "속커튼"],
    "수예":       ["십자수키트", "니팅세트", "펠트공예키트", "자수틀세트", "코바늘세트"],
    "솜류":       ["충전재솜", "인형솜", "쿠션솜", "누빔솜", "폴리에스터솜"],
    "DIY자재/용품": ["레진공예재료", "우드공예키트", "액자DIY세트", "캔들만들기재료", "비즈공예세트"],
    "수납가구":     ["접이식수납장", "미니서랍장", "자취용선반", "원룸수납장", "폴딩박스수납"],

    # ── 스포츠/레저 ────────────────────────────────────────────────────
    "요가/필라테스": ["필라테스양말", "요가블록세트", "폼롤러소형", "요가스트랩", "밸런스쿠션"],
    "스포츠액세서리": ["스포츠헤어밴드", "손목보호대운동", "운동장갑여성", "스포츠물병", "요가매트가방"],
    "보호용품":     ["무릎보호대", "손목보호대", "팔꿈치보호대", "허리보호대", "발목보호대"],
}

# 카테고리 이탈 방지용 어근
CATEGORY_ROOTS = {
    "가방소품":     ["가방고리", "가방참", "가방체인", "네임택", "가방정리"],
    "모자":        ["모자", "햇", "캡", "비니", "버킷", "볼캡"],
    "벨트":        ["벨트"],
    "신발용품":     ["신발", "구두", "깔창", "슈즈케어", "신발끈"],
    "양말":        ["양말", "삭스", "크루삭스", "레이스양말", "타비"],
    "여성가방":     ["가방", "백", "크로스백", "버킷백", "토트", "숄더"],
    "여성신발":     ["플랫", "슈즈", "부츠", "스니커즈", "구두"],
    "여행용가방/소품": ["여행", "캐리어", "파우치", "여권"],
    "장갑":        ["장갑"],
    "주얼리":      ["목걸이", "반지", "귀걸이", "팔찌", "이어링", "주얼리", "이어커프"],
    "지갑":        ["지갑", "카드홀더", "카드지갑"],
    "패션소품":     ["브로치", "헤어밴드", "선글라스체인", "손수건", "벨트백"],
    "헤어액세서리":  ["헤어핀", "헤어클립", "집게핀", "헤어밴드", "머리끈", "Y2K"],
    "여성의류":     ["원피스", "가디건", "팬츠", "크롭탑", "코트", "블라우스", "니트"],
    "여성언더웨어/잠옷": ["잠옷", "속옷", "언더웨어", "홈웨어", "브라렛"],
    "뷰티소품":     ["뷰티", "메이크업", "블렌더", "퍼프", "스펀지", "클렌징패드"],
    "네일케어":     ["큐티클", "네일아트", "네일스티커", "젤네일", "발톱", "손톱", "버퍼"],
    "헤어스타일링":  ["헤어롤러", "롤빗", "고데기", "헤어핀셋", "앞머리클립"],
    "헤어케어":     ["빗", "두피", "브러쉬", "헤어터번", "헤어에센스"],
    "수납/정리용품": ["정리함", "수납", "정리대", "정리박스", "정리트레이"],
    "문구/사무용품": ["마스킹테이프", "스티커", "다이어리", "떡메모지", "책갈피"],
    "발건강용품":   ["족욕", "발뒤꿈치", "발각질", "종아리", "발가락"],
    "좌욕/좌훈용품": ["좌욕", "좌훈"],
    "반려동물":     ["장난감", "노즈워크", "캣타워", "터그", "터널", "낚시대", "고양이", "강아지"],
    "원예/식물":    ["화분", "식물", "다육이", "수경재배"],
    "생활용품":     ["밀폐용기", "정리바구니", "다용도수납", "냄비받침"],
    "세탁용품":     ["세탁", "빨래", "섬유탈취제", "울샴푸"],
    "청소용품":     ["청소", "걸레", "청소솔", "스퀴지"],
    "건강관리용품":  ["폼롤러", "스트레칭밴드", "압박스타킹", "영양제보관함"],
    "욕실용품":     ["욕실", "샤워", "비누받침", "칫솔꽂이"],
    "인테리어소품":  ["무드등", "액자", "드라이플라워", "캔들홀더", "화분", "인테리어"],
    "홈데코":      ["타피스트리", "가랜드", "벽거울", "홈데코", "리스장식"],
    "침구단품":     ["이불", "매트리스커버", "이불커버"],
    "침구세트":     ["침구세트"],
    "베개":       ["베개"],
    "카페트/러그":   ["러그", "카페트"],
    "커튼/블라인드": ["커튼", "블라인드", "롤스크린"],
    "수예":       ["십자수", "니팅", "펠트", "자수", "코바늘", "수예"],
    "솜류":       ["솜"],
    "DIY자재/용품": ["DIY", "공예", "레진", "비즈"],
    "수납가구":     ["수납장", "서랍장", "선반", "수납박스"],
    "요가/필라테스": ["필라테스", "요가", "폼롤러", "스트랩", "밸런스"],
    "스포츠액세서리": ["스포츠헤어밴드", "손목보호대", "운동장갑", "스포츠물병", "요가매트가방"],
    "보호용품":     ["보호대"],
}


@dataclass
class KeywordData:
    keyword: str
    monthly_pc: int        # 월간 PC 검색수
    monthly_mobile: int    # 월간 모바일 검색수
    monthly_clicks: int = 0  # 월평균 클릭수 (PC+모바일) — 구매의도 신호
    comp_idx: str = ""     # 광고 경쟁지수 (낮음/중간/높음)
    ratio: float = 0.0     # 트렌드 상대 지수 (trend.py 에서 보완)
    trend_avg: float = 0.0
    rank: int = 0

    @property
    def monthly_total(self) -> int:
        return self.monthly_pc + self.monthly_mobile

    @property
    def click_rate(self) -> float:
        """검색 대비 클릭률 (구매의도 근사)."""
        total = self.monthly_total
        return (self.monthly_clicks / total) if total > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.rank:>2}위 | {self.keyword:<22} "
            f"| 월검색: {self.monthly_total:>6,}건 (PC {self.monthly_pc:,} + 모바일 {self.monthly_mobile:,})"
        )


def _sign(method: str, uri: str, secret_key: str) -> tuple:
    """HMAC-SHA256 서명 생성. (timestamp, signature) 반환."""
    ts = str(int(time.time() * 1000))
    message = f"{ts}.{method}.{uri}".encode("utf-8")
    sig = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).digest()
    ).decode("utf-8")
    return ts, sig


def _headers(method: str, uri: str, api_key: str, secret_key: str, customer_id: str) -> dict:
    ts, sig = _sign(method, uri, secret_key)
    return {
        "X-Timestamp": ts,
        "X-API-KEY": api_key,
        "X-Customer": str(customer_id),
        "X-Signature": sig,
    }


def fetch_related_keywords(
    seed: str,
    limit: int = 100,
    api_key: str = "",
    secret_key: str = "",
    customer_id: str = "",
) -> List[KeywordData]:
    """
    시드 키워드 1개 → 관련 키워드 목록 + 월간 검색수 반환.
    """
    if not _HAS_REQUESTS:
        raise NotImplementedError("pip3 install requests 후 재시도하세요.")

    ak = api_key or os.environ.get("NAVER_AD_API_KEY", "")
    sk = secret_key or os.environ.get("NAVER_AD_SECRET_KEY", "")
    cid = customer_id or os.environ.get("NAVER_AD_CUSTOMER_ID", "")
    if not (ak and sk and cid):
        raise ValueError(".env에 NAVER_AD_API_KEY, NAVER_AD_SECRET_KEY, NAVER_AD_CUSTOMER_ID를 설정하세요.")

    uri = "/keywordstool"
    params = {"hintKeywords": seed, "showDetail": "1"}
    hdrs = _headers("GET", uri, ak, sk, cid)

    resp = requests.get(_BASE_URL + uri, headers=hdrs, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("keywordList", []):
        kw = item.get("relKeyword", "")
        pc = _parse_count(item.get("monthlyPcQcCnt", 0))
        mob = _parse_count(item.get("monthlyMobileQcCnt", 0))
        clk = _parse_count(item.get("monthlyAvePcClkCnt", 0)) + _parse_count(item.get("monthlyAveMobileClkCnt", 0))
        comp = str(item.get("compIdx", "")).strip()
        if kw and (pc + mob) > 0:
            results.append(KeywordData(
                keyword=kw, monthly_pc=pc, monthly_mobile=mob,
                monthly_clicks=clk, comp_idx=comp,
            ))

    # 월간 검색수 내림차순 정렬
    results.sort(key=lambda k: k.monthly_total, reverse=True)
    for rank, kd in enumerate(results[:limit], 1):
        kd.rank = rank

    return results[:limit]


def _is_relevant(keyword: str, category: str) -> bool:
    """재귀 확장 시 카테고리 이탈 방지 — 어근 중 하나라도 포함하면 채택."""
    roots = CATEGORY_ROOTS.get(category, [])
    if not roots:
        return True
    return any(root in keyword for root in roots)


def discover_keywords(
    category: str,
    depth: int = EXPANSION_DEPTH,
    api_key: str = "",
    secret_key: str = "",
    customer_id: str = "",
) -> List[KeywordData]:
    """
    재귀 확장으로 카테고리 롱테일 키워드 풀을 발굴.

    Level 0: CATEGORY_SEEDS[category] 전체(5개)를 시드로 시작.
    각 레벨: 시드별 관련 키워드 수집 → 카테고리 어근 가드 통과분만 누적.
    다음 레벨 시드: 후보를 "어느 level-0 시드에서 파생됐는지"(origin branch)별로
                    묶어서, 재투입 검색수 구간(RESEED_MIN~MAX)에 드는 롱테일
                    키워드를 branch마다 균등 할당(quota)해서 선택한다.
                    (2026-07 수정: 예전엔 전체를 검색수 오름차순으로 한 번에
                    잘랐는데, 특정 branch가 재투입 구간에 후보를 더 많이
                    내놓으면 그 branch가 다음 레벨을 독식해서 몇 단계 안에
                    카테고리 전체가 한 하위주제로 수렴해버리는 문제가 실측
                    확인됨 — 예: '셀프케어' 5갈래가 전부 '발각질'로 붕괴.)

    정렬은 호출부(discover)에서 골든레이시오로 수행 — 여기서는 dedup된 풀만 반환.
    """
    seeds = CATEGORY_SEEDS.get(category)
    if not seeds:
        raise ValueError(f"지원하지 않는 카테고리: '{category}'. 가능: {list(CATEGORY_SEEDS.keys())}")

    seen: dict = {}
    # (검색할 키워드, origin branch) — origin은 level-0 시드 = 하위주제 식별자
    current_seeds = [(s, s) for s in seeds]   # Level 0: 5개 시드 전부 사용
    used_seeds = set()

    for level in range(max(1, depth)):
        next_candidates: List[tuple] = []  # (KeywordData, origin)
        for seed, origin in current_seeds:
            if seed in used_seeds:
                continue
            used_seeds.add(seed)
            try:
                kw_list = fetch_related_keywords(
                    seed, limit=100,
                    api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                )
            except Exception as e:
                print(f"  [경고] 시드 '{seed}' 탐색 실패: {e}")
                continue
            for kd in kw_list:
                if kd.keyword in seen:
                    continue
                if not _is_relevant(kd.keyword, category):
                    continue
                seen[kd.keyword] = kd
                next_candidates.append((kd, origin))
            time.sleep(0.3)

        # branch(origin)별로 재투입 후보를 묶는다
        by_origin: dict = {}
        for kd, origin in next_candidates:
            if RESEED_MIN_SEARCH <= kd.monthly_total <= RESEED_MAX_SEARCH:
                by_origin.setdefault(origin, []).append(kd)

        if not by_origin:
            break

        # branch마다 균등 quota로 롱테일(검색수 오름차순) 선택 — 한 branch가
        # 다음 레벨 시드를 독식하지 못하게 해서 하위주제 다양성을 유지한다
        quota = max(1, RESEED_PER_LEVEL // len(by_origin))
        next_seeds = []
        for origin, kds in by_origin.items():
            kds.sort(key=lambda k: k.monthly_total)
            for kd in kds[:quota]:
                next_seeds.append((kd.keyword, origin))
        current_seeds = next_seeds

    return list(seen.values())


def fetch_category_keywords(
    category: str,
    limit: int = 50,
    min_search: int = 500,
    max_search: int = 50000,
    api_key: str = "",
    secret_key: str = "",
    customer_id: str = "",
    client_id: str = "",        # 미사용 (호환성 유지)
    client_secret: str = "",    # 미사용 (호환성 유지)
) -> List[KeywordData]:
    """
    (하위호환) 단일 레벨 카테고리 키워드 수집 → 검색수 필터링 → 검색수 내림차순.
    신규 파이프라인은 discover_keywords()를 사용한다.
    """
    kds = discover_keywords(category, depth=1,
                            api_key=api_key, secret_key=secret_key, customer_id=customer_id)
    filtered = [kd for kd in kds if min_search <= kd.monthly_total <= max_search]
    filtered.sort(key=lambda k: k.monthly_total, reverse=True)
    for rank, kd in enumerate(filtered[:limit], 1):
        kd.rank = rank
    return filtered[:limit]


def _parse_count(val) -> int:
    """'< 10' 같은 문자열도 처리."""
    if isinstance(val, int):
        return val
    s = str(val).replace(",", "").strip()
    if s.startswith("<"):
        return 5   # '< 10' → 5로 처리
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0
