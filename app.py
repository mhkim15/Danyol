"""
비브레이브 소싱 대시보드 — Streamlit UI
실행: python3 -m streamlit run app.py
"""
import json
import os
import sys
import time
from pathlib import Path

import streamlit as st
import pandas as pd

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()
sys.path.insert(0, str(Path(__file__).parent))

DATA_DIR      = Path(__file__).parent / "data"
SELECTED_FILE = DATA_DIR / "selected_products.json"
SOURCING_LOG  = DATA_DIR / "sourcing_log.json"


# ── 공통 유틸 ──────────────────────────────────────────────────────────────────
def load_selected():
    if not SELECTED_FILE.exists():
        return []
    with open(SELECTED_FILE, encoding="utf-8") as f:
        return json.load(f)


def add_to_selected(items):
    existing = load_selected()
    existing_kws = {p["keyword"] for p in existing}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    for r in items:
        if r.keyword not in existing_kws:
            existing.append({
                "keyword":        r.keyword,
                "category":       r.category,
                "supply_price":   r.supply_price,
                "supply_name":    r.supply_name,
                "naver_price":    int(r.avg_naver_price) if r.avg_naver_price else 0,
                "margin_rate":    round(r.margin_rate, 4) if r.margin_rate else 0,
                "golden_ratio":   r.golden_ratio,
                "recommendation": r.recommendation,
            })
            existing_kws.add(r.keyword)
            added += 1
    with open(SELECTED_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added


# ── 페이지 설정 ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="비브레이브 소싱 대시보드",
    page_icon="🛒",
    layout="wide",
)

# ── 사이드바 ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🛒 비브레이브")
    st.caption("소싱 자동화 대시보드")
    st.divider()
    page = st.radio(
        "메뉴",
        ["🔍 상품 탐색", "📋 선택 목록", "📊 소싱 로그"],
        label_visibility="collapsed",
    )
    st.divider()
    selected_count = len(load_selected())
    st.metric("선택된 상품", f"{selected_count}개")

# ══════════════════════════════════════════════════════════════════════════════
# 페이지 1: 상품 탐색
# ══════════════════════════════════════════════════════════════════════════════
if page == "🔍 상품 탐색":
    st.title("🔍 소싱 상품 탐색")
    st.caption("카테고리를 선택하고 탐색을 실행하면 경쟁력 있는 상품을 추천합니다.")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        category = st.selectbox("카테고리", ["주방용품", "욕실용품", "생활용품", "정리수납"])
    with col2:
        max_supply = st.number_input("최대 등록상품수", value=10_000, step=1_000,
                                     min_value=1_000, max_value=30_000)
    with col3:
        limit = st.number_input("결과 수", value=20, step=5, min_value=5, max_value=50)
    with col4:
        depth = st.number_input("키워드 확장 깊이", value=2, step=1, min_value=1, max_value=3)

    use_domemae = st.checkbox("도매매 가격 조회 (마진 계산)", value=True)
    run_btn = st.button("🚀 탐색 시작", type="primary", use_container_width=True)

    st.divider()

    if run_btn:
        from bebrave.sourcing.discover import discover
        with st.spinner("탐색 중... (약 1~3분 소요)"):
            try:
                t0 = time.time()
                results = discover(
                    category=category,
                    limit=limit,
                    with_domemae=use_domemae,
                    depth=depth,
                    max_supply=max_supply,
                )
                elapsed = time.time() - t0
                st.session_state["results"]  = results
                st.session_state["category"] = category
                st.success(f"탐색 완료 — {len(results)}개 키워드 분석 ({elapsed:.0f}초)")
            except Exception as e:
                st.error(f"탐색 오류: {e}")
                st.stop()

    results  = st.session_state.get("results", [])
    category = st.session_state.get("category", "")

    if results:
        recommended = [r for r in results if r.recommendation == "진입 권장"]
        possible    = [r for r in results if r.recommendation == "진입 가능"]
        excluded    = [r for r in results if r.recommendation in ("보류", "제외")]

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("전체 분석", f"{len(results)}개")
        m2.metric("⭐ 진입 권장", f"{len(recommended)}개")
        m3.metric("✅ 진입 가능", f"{len(possible)}개")
        m4.metric("❌ 보류/제외", f"{len(excluded)}개")

        st.divider()

        def render_table(items, tab_key):
            if not items:
                st.info("해당 항목이 없습니다.")
                return

            df = pd.DataFrame([{
                "키워드":        r.keyword,
                "점수":          r.score,
                "레이시오":      r.golden_ratio,
                "등록상품수":    r.product_count,
                "트렌드":        {"up":"↑상승","stable":"→안정","down":"↓하락"}.get(r.trend_direction, "-"),
                "계절성":        "★" if r.is_seasonal else "",
                "네이버 평균가": int(r.avg_naver_price) if r.avg_naver_price else 0,
                "도매가":        r.supply_price if r.supply_price else 0,
                "마진율":        f"{r.margin_rate:.1%}" if r.margin_rate else "-",
                "공급사":        (r.supply_name or "")[:25],
                "판단":          r.recommendation,
            } for r in items])

            event = st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key=f"df_{tab_key}",
                column_config={
                    "네이버 평균가": st.column_config.NumberColumn(format="%d원"),
                    "도매가":       st.column_config.NumberColumn(format="%d원"),
                    "등록상품수":   st.column_config.NumberColumn(format="%d개"),
                },
            )

            sel_rows = event.selection.rows if hasattr(event, "selection") else []
            if sel_rows:
                sel_items = [items[i] for i in sel_rows]
                st.info(f"선택: {', '.join(r.keyword for r in sel_items)}")
                if st.button(f"📌 선택 항목 등록 목록에 추가", key=f"add_{tab_key}", type="secondary"):
                    n = add_to_selected(sel_items)
                    st.success(f"{n}개 추가 완료! (중복 제외)")
                    st.rerun()

        tab1, tab2, tab3 = st.tabs([
            f"⭐ 진입 권장 ({len(recommended)})",
            f"✅ 진입 가능 ({len(possible)})",
            f"❌ 보류/제외 ({len(excluded)})",
        ])
        with tab1: render_table(recommended, "rec")
        with tab2: render_table(possible, "pos")
        with tab3: render_table(excluded, "exc")

# ══════════════════════════════════════════════════════════════════════════════
# 페이지 2: 선택 목록
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📋 선택 목록":
    st.title("📋 등록 선택 목록")
    st.caption("탐색에서 선택한 상품 목록입니다. 스마트스토어 등록 전 최종 확인하세요.")

    selected = load_selected()

    if not selected:
        st.info("선택된 상품이 없습니다. '상품 탐색' 탭에서 상품을 선택해 주세요.")
    else:
        df = pd.DataFrame([{
            "키워드":        p["keyword"],
            "카테고리":      p.get("category", "-"),
            "도매가":        p.get("supply_price", 0),
            "네이버 평균가": p.get("naver_price", 0),
            "마진율":        f"{p.get('margin_rate', 0):.1%}",
            "레이시오":      p.get("golden_ratio", 0),
            "공급사":        (p.get("supply_name") or "-")[:25],
            "추천 등급":     p.get("recommendation", "-"),
        } for p in selected])

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "도매가":        st.column_config.NumberColumn(format="%d원"),
                "네이버 평균가": st.column_config.NumberColumn(format="%d원"),
            },
        )

        col1, col2 = st.columns([1, 5])
        with col1:
            if st.button("🗑️ 목록 초기화", type="secondary"):
                SELECTED_FILE.write_text("[]", encoding="utf-8")
                st.success("초기화 완료")
                st.rerun()
        with col2:
            st.caption(f"총 {len(selected)}개 선택됨")

        st.divider()
        st.info("💡 네이버 커머스 API 연동 후 '스마트스토어 등록' 버튼이 활성화됩니다.")

# ══════════════════════════════════════════════════════════════════════════════
# 페이지 3: 소싱 로그
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📊 소싱 로그":
    st.title("📊 소싱 로그")
    st.caption("이전 탐색에서 저장된 전체 키워드 이력입니다.")

    if not SOURCING_LOG.exists():
        st.info("소싱 로그가 없습니다. 탐색을 먼저 실행하세요.")
    else:
        with open(SOURCING_LOG, encoding="utf-8") as f:
            data = json.load(f)

        if not data:
            st.info("저장된 소싱 후보가 없습니다.")
        else:
            df = pd.DataFrame([{
                "키워드":       d["keyword"],
                "카테고리":     d.get("category", "-"),
                "점수":         d.get("score", 0),
                "등록상품수":   d.get("product_count", 0),
                "도매가":       d.get("est_cost_price", 0),
                "판매가(추정)": d.get("est_sale_price", 0),
                "추가일":       d.get("added_date", "-"),
                "메모":         (d.get("notes") or "")[:40],
            } for d in data])

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "도매가":       st.column_config.NumberColumn(format="%d원"),
                    "판매가(추정)": st.column_config.NumberColumn(format="%d원"),
                    "등록상품수":   st.column_config.NumberColumn(format="%d개"),
                },
            )
            st.caption(f"총 {len(data)}개 저장됨")
