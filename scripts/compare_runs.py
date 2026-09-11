"""검색 run 두 개를 **문항 단위로** 비교한다 — 채택 판정용.

`eval_retrieval.py` 의 게이트는 집계(recall@5·MRR)가 기준선보다 떨어졌나만 본다. 그런데
전처리 옵션의 채택 기준은 두 가지다:

    ① 개선이 있나      Recall@5 ≥ +0.03
    ② 아무도 안 망가졌나  verified 문항 중 hit@5 → miss 전환 0건

②는 집계로는 안 보인다 — 한 문항이 떨어지고 다른 문항이 올라가면 평균은 그대로다.
그래서 두 run 의 tb_eval_item 을 질의(item_id)로 조인해 문항마다 순위를 대조한다.

⚠ **①은 이 골든에서 달성이 불가능하다.** 기준선의 verified recall@5 가 이미 1.000(천장)이라
어떤 변경도 +0.03 을 못 만든다. 기준을 문자 그대로 적용하면 모든 옵션이 "기각"된다.
판별 신호는 포화되지 않은 축에 있다 — recall@1 과 MRR, 그리고 옵션이 겨냥한 축(표 캡션이면
table_fact). 이 도구는 문자 그대로의 기준과 미포화 신호를 **나란히** 출력한다. 어느 쪽으로
판정할지는 사람이 정한다(기준을 조용히 바꾸지 않는다).

용법 (api 컨테이너 안 — DB 에 닿는 곳):
    python /app/scripts/compare_runs.py                     # 가장 최근 두 retrieval run
    python /app/scripts/compare_runs.py --base <run_id> --new <run_id>
종료 코드: verified 문항에 hit@5→miss 가 하나라도 있으면 1.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

KS = (1, 3, 5, 10)


def _load(db, run_id: str) -> dict[str, dict]:
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT item_id, scores_json, detail_json FROM tb_eval_item WHERE run_id = :r"),
        {"r": run_id}).all()
    out = {}
    for item_id, scores, detail in rows:
        s = scores if isinstance(scores, dict) else json.loads(scores or "{}")
        d = detail if isinstance(detail, dict) else json.loads(detail or "{}")
        out[item_id] = {"rank": s.get("rank"), "dup": s.get("dup_at10"),
                        "axis": d.get("axis") or "clause", "verified": bool(d.get("verified"))}
    return out


def _hit(rank, k: int) -> bool:
    return rank is not None and rank <= k


def _agg(items: list[dict]) -> dict:
    n = len(items)
    if not n:
        return {"n": 0}
    dups = [i["dup"] for i in items if i["dup"] is not None]
    return {"n": n,
            "recall": {k: sum(_hit(i["rank"], k) for i in items) / n for k in KS},
            "mrr": sum((1 / i["rank"]) if i["rank"] else 0.0 for i in items) / n,
            "dup10": (sum(dups) / len(dups)) if dups else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="기준 run_id (기본: 두 번째로 최근 retrieval run)")
    ap.add_argument("--new", help="비교 run_id (기본: 가장 최근 retrieval run)")
    a = ap.parse_args()

    from sqlalchemy import text

    from v1.config import task_session

    with task_session() as db:
        if not (a.base and a.new):
            recent = [r[0] for r in db.execute(text(
                "SELECT run_id FROM tb_eval_run WHERE kind = 'retrieval' "
                "ORDER BY created_at DESC LIMIT 2")).all()]
            if len(recent) < 2 and not (a.base and a.new):
                print("  비교할 retrieval run 이 2개 미만"); return 2
            a.new = a.new or recent[0]
            a.base = a.base or recent[1]
        shas = dict(db.execute(text(
            "SELECT run_id, git_sha FROM tb_eval_run WHERE run_id IN (:b, :n)"),
            {"b": a.base, "n": a.new}).all())
        base, new = _load(db, a.base), _load(db, a.new)

    common = sorted(set(base) & set(new))
    only_b, only_n = set(base) - set(new), set(new) - set(base)
    print(f"  base {a.base} (git {shas.get(a.base, '?')})")
    print(f"  new  {a.new} (git {shas.get(a.new, '?')})")
    print(f"  공통 문항 {len(common)}" + (f" · base 전용 {len(only_b)} · new 전용 {len(only_n)}"
                                        if only_b or only_n else ""))

    # ── 축별 집계 대조 ─────────────────────────────────────────────────────────
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for q in common:
        groups["verified(게이트)" if base[q]["verified"] else "_unverified"].append(q)
        groups[f"axis:{base[q]['axis']}"].append(q)
        groups["전체"].append(q)

    print(f"\n  {'구분':<18}{'n':>4}  {'r@1':>13}  {'r@5':>13}  {'MRR':>13}  {'dup@10':>11}")
    print("  " + "─" * 80)
    order = ["전체", "verified(게이트)"] + sorted(g for g in groups if g.startswith("axis:"))
    for g in order:
        qs = groups.get(g) or []
        if not qs:
            continue
        b, n = _agg([base[q] for q in qs]), _agg([new[q] for q in qs])

        def cell(x, y):
            d = y - x
            return f"{x:.3f}→{y:.3f}{'↑' if d > 1e-9 else ('↓' if d < -1e-9 else ' ')}"
        dup = (f"{b['dup10']:.1f}→{n['dup10']:.1f}" if b["dup10"] is not None
               and n["dup10"] is not None else "—")
        print(f"  {g:<18}{len(qs):>4}  {cell(b['recall'][1], n['recall'][1]):>13}  "
              f"{cell(b['recall'][5], n['recall'][5]):>13}  {cell(b['mrr'], n['mrr']):>13}  {dup:>11}")

    # ── 문항 단위 전환 ─────────────────────────────────────────────────────────
    broke = [q for q in common if _hit(base[q]["rank"], 5) and not _hit(new[q]["rank"], 5)]
    fixed = [q for q in common if not _hit(base[q]["rank"], 5) and _hit(new[q]["rank"], 5)]
    moved = sorted(((q, base[q]["rank"], new[q]["rank"]) for q in common
                    if base[q]["rank"] != new[q]["rank"]),
                   key=lambda t: ((t[2] or 99) - (t[1] or 99)))
    broke_v = [q for q in broke if base[q]["verified"]]

    print(f"\n  hit@5 → miss  {len(broke)}건 (verified {len(broke_v)})   ·   miss → hit@5  {len(fixed)}건")
    for q in broke:
        tag = "verified" if base[q]["verified"] else "초안"
        print(f"    ❌ [{tag}] {base[q]['rank']}위 → {new[q]['rank'] or '밖'}  {q[:60]}")
    if moved:
        print(f"\n  순위가 바뀐 문항 {len(moved)}건 (개선 → 악화 순):")
        for q, rb, rn in moved[:12]:
            arrow = "▲" if (rn or 99) < (rb or 99) else "▼"
            print(f"    {arrow} {str(rb or '밖'):>3} → {str(rn or '밖'):<3} [{base[q]['axis']}] {q[:54]}")

    # ── 판정 ──────────────────────────────────────────────────────────────────
    vb = _agg([base[q] for q in groups["verified(게이트)"]])
    vn = _agg([new[q] for q in groups["verified(게이트)"]])
    d5 = vn["recall"][5] - vb["recall"][5]
    print("\n  ── 채택 기준 (문자 그대로) ──")
    print(f"    ① verified Recall@5 Δ = {d5:+.3f}  (기준 ≥ +0.030)  "
          f"{'✅' if d5 >= 0.03 - 1e-9 else '❌'}"
          + ("   ⚠ 기준선이 1.000 천장 — 구조적으로 달성 불가" if vb["recall"][5] >= 1 - 1e-9 else ""))
    print(f"    ② verified hit@5→miss = {len(broke_v)}건  (기준 0)  {'✅' if not broke_v else '❌'}")
    print("  ── 미포화 신호 (참고) ──")
    print(f"    verified  Δr@1 = {vn['recall'][1] - vb['recall'][1]:+.3f}   ΔMRR = {vn['mrr'] - vb['mrr']:+.3f}")
    for g in order:
        if g.startswith("axis:") and groups.get(g):
            b, n = _agg([base[q] for q in groups[g]]), _agg([new[q] for q in groups[g]])
            print(f"    {g:<17} Δr@1 = {n['recall'][1] - b['recall'][1]:+.3f}   "
                  f"ΔMRR = {n['mrr'] - b['mrr']:+.3f}   (n={b['n']}, 초안 포함)")
    return 1 if broke_v else 0


if __name__ == "__main__":
    sys.exit(main())
