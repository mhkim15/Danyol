"""상품별 조치 이력과 효과 — "언제 무엇을 했고, 그 뒤 팔림이 달라졌나".

이름 재최적화만 따로 원장(name_changes.json)이 있었고, 가격 변경·판매중지·교체는 한 일이
어디에도 남지 않았다(2026-10). 네이버에 실제로 반영된 조치는 전부 여기 한 원장에 적는다.

효과 비교는 같은 길이로 자른다 — 조치 뒤 N일(최대 30일)과 조치 앞 N일의 주문을 비교한다.
교체(replaced)는 앞은 옛 상품, 뒤는 새 상품의 주문을 본다.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ACTION_LOG = Path("data/product_actions.json")
MAX_WINDOW_DAYS = 30

KIND_LABEL = {"suspend": "판매중지", "resume": "판매 재개", "price": "가격 변경", "rename": "이름 변경",
              "sync": "도매매 반영", "replaced": "교체", "replacement": "교체 등록"}


def load() -> list:
    rows = []
    if ACTION_LOG.exists():
        with open(ACTION_LOG, encoding="utf-8") as f:
            rows = json.load(f)
    # 예전 이름 변경 원장도 같은 이력으로 보여준다(읽기만, 같은 날 같은 상품 이름 변경은 한 번만)
    from .name_changes import load_name_changes
    seen = {(r["naver_product_id"], r["at"]) for r in rows if r["kind"] == "rename"}
    for c in load_name_changes():
        if (c["naver_product_id"], c["changed_at"]) not in seen:
            rows.append({"naver_product_id": c["naver_product_id"], "kind": "rename", "at": c["changed_at"],
                         "summary": f"{c['old_name'][:18]} → {c['new_name'][:18]}"})
    return rows


def record(naver_product_id: str, kind: str, summary: str, **extra) -> None:
    rows = []
    if ACTION_LOG.exists():
        with open(ACTION_LOG, encoding="utf-8") as f:
            rows = json.load(f)
    rows.append({"naver_product_id": str(naver_product_id), "kind": kind, "summary": summary,
                 "at": date.today().isoformat(), **extra})
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ACTION_LOG, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _orders(sales: list, pid: str, start: date, end: date) -> tuple:
    hit = [r for r in sales if str(r.get("naver_product_id")) == pid and start.isoformat() <= r["date"] < end.isoformat()]
    return len(hit), sum(r.get("revenue", 0) for r in hit)


def effect(entry: dict, sales: list, today: Optional[date] = None) -> Optional[dict]:
    """조치 앞뒤 같은 길이 구간의 주문 비교. 조치 당일이면 None(아직 볼 게 없음)."""
    today = today or date.today()
    at = date.fromisoformat(entry["at"])
    window = min((today - at).days, MAX_WINDOW_DAYS)
    if window < 1:
        return None
    pid = entry["naver_product_id"]
    after_pid = (entry.get("new_product_id") or pid) if entry["kind"] == "replaced" else pid
    b_n, b_rev = _orders(sales, pid, at - timedelta(days=window), at)
    a_n, a_rev = _orders(sales, str(after_pid), at, at + timedelta(days=window))
    return {"days": window, "before_orders": b_n, "before_revenue": b_rev, "after_orders": a_n, "after_revenue": a_rev,
            "better": a_n > b_n if a_n != b_n else None}


def history(naver_product_id: str, sales: list, rows: Optional[list] = None, today: Optional[date] = None) -> list:
    """이 상품의 조치 이력, 최근 것부터 — 각 항목에 label·effect를 붙인다."""
    rows = load() if rows is None else rows
    mine = [dict(r, label=KIND_LABEL.get(r["kind"], r["kind"])) for r in rows
            if r["naver_product_id"] == str(naver_product_id)]
    mine.sort(key=lambda r: r["at"], reverse=True)
    for r in mine:
        r["effect"] = effect(r, sales, today)
    return mine


def _demo() -> None:
    """실행 가능한 자체 점검 — 같은 길이 구간 비교와 교체 시 새 상품 주문으로 보는지 (파일 IO 없음)."""
    today = date(2026, 10, 20)
    sales = [{"naver_product_id": "1", "date": "2026-10-05", "revenue": 1000},   # 조치 전(10일 구간 안)
             {"naver_product_id": "1", "date": "2026-09-01", "revenue": 9999},   # 구간 밖 — 세면 안 됨
             {"naver_product_id": "1", "date": "2026-10-12", "revenue": 2000},
             {"naver_product_id": "1", "date": "2026-10-15", "revenue": 2000},
             {"naver_product_id": "2", "date": "2026-10-13", "revenue": 5000}]
    e = effect({"naver_product_id": "1", "kind": "price", "at": "2026-10-10"}, sales, today)
    assert e["days"] == 10 and e["before_orders"] == 1 and e["after_orders"] == 2 and e["better"] is True, e
    e = effect({"naver_product_id": "1", "kind": "replaced", "new_product_id": "2", "at": "2026-10-10"}, sales, today)
    assert e["before_orders"] == 1 and e["after_orders"] == 1 and e["after_revenue"] == 5000, e
    assert effect({"naver_product_id": "1", "kind": "price", "at": "2026-10-20"}, sales, today) is None
    h = history("1", sales, [{"naver_product_id": "1", "kind": "suspend", "summary": "", "at": "2026-10-01"},
                             {"naver_product_id": "1", "kind": "price", "summary": "", "at": "2026-10-10"}], today)
    assert [r["kind"] for r in h] == ["price", "suspend"] and h[0]["label"] == "가격 변경"
    print("action_log self-check OK")


if __name__ == "__main__":
    _demo()
