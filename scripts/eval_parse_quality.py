"""V1 — 파싱 불변 조건. **정답 없이** 파싱이 깨졌는지 잡는다.

**왜 정답 없이 되나**: 원본 PDF 의 텍스트 레이어가 곧 기준선이다. ODL 이 그걸 얼마나
옮겼는지(char_coverage), 구조 표지를 얼마나 살렸는지(anchor_coverage)를 재면
"정답 문서"를 사람이 만들지 않아도 파손을 잡을 수 있다. 이게 골든셋보다 먼저 와야 하는
이유이기도 하다 — **파싱이 깨진 문서를 알고 나서 골든 문항을 뽑아야** 검색 실패를
파싱 탓과 구분할 수 있다.

지표 다섯:
    char_coverage    ODL 글자 수 / PyMuPDF 글자 수 (페이지별 → 문서 중앙값·최솟값)
    anchor_coverage  ODL 이 살린 제N조 / PDF 원문의 제N조 (누락 조 목록도 낸다)
    article_order    조 번호 오름차순 비율 — 다단 뒤섞임이 여기 걸린다
    table_agreement  |ODL table 블록 수 − pdfplumber find_tables 수| (페이지별)
    empty_or_sparse  글자 밀도가 문서 중앙값의 10% 미만인 NATIVE 페이지 수

⚠ **비교 전에 정규화한다.** 이 코퍼스는 \\x01~\\x08 을 단어 구분자로 쓰고(38% 페이지,
비율 중앙 0.157) ODL 과 PyMuPDF 가 그걸 다르게 흘린다. 정규화 안 하면 멀쩡한 페이지가
커버리지 미달로 잡힌다.

⚠ **벤치 문서는 범위 밖.** 이 지표는 약관 구조를 전제한다(제N조). 고시·공고문은 조가
없어서 anchor_coverage 가 정의되지 않는다 — 코퍼스와 같은 자로 재면 안 된다.

용법:
    python /app/scripts/eval_parse_quality.py --distribution        # 분포만 (임계 없음)
    python /app/scripts/eval_parse_quality.py --distribution --doc R09
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import statistics as st
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 벤치 재료(고시·공고문) — 약관 규칙의 대상이 아니다. raw_bench 분리와 같은 기준.
BENCH_DOCS = {"B005", "D14101", "D14102", "D14103"}

# ── 게이트 임계 ───────────────────────────────────────────────────────────────
# **전부 코퍼스 분포의 빈 구간에 앉혔다.** 칼날 위가 아니라는 뜻이고, 그게 이 값들을
# 믿는 근거다. 지시서 초안(char_coverage 최솟값 < 0.9)은 22문서 중 20개를 실패시켰는데,
# 원인이 임계가 아니라 지표였다(조판 중복을 파싱 손실로 셌다 — unique_chars 주석).
THRESHOLDS = {
    # 조가 통째로 사라진 건 명백한 파손이다. 외부 법령 인용을 제외하고 재면
    # **현재 코퍼스 22/22 가 누락 0** — 지금은 안 울리고 깨지면 울리는, 게이트의 이상적 형태.
    "anchor_missing_max": 0,
    # 빈 구간 0.974(R10) → 0.996. 문서 전체가 얇아진 경우를 잡는다.
    "char_cov_median_min": 0.99,
    # 빈 구간 0.752 → 0.828. 페이지 단위 국소 손실은 문서 파손과 다르므로 WARN.
    "char_cov_min_warn": 0.79,
    # ⚠ article_order·sparse·table_diff 는 **임계를 두지 않는다.** 분포에 빈 구간이 없다
    #   (article_order 0.632~0.761 연속) — 특약마다 제1조부터 다시 시작하는 약관 구조에서
    #   오름차순이 깨지는 게 정상이기 때문. 절대 임계로 쓰면 전 문서가 걸리거나 아무도
    #   안 걸린다. 기준선 대비 **하락**만 본다(그래서 run 을 저장한다).
}

_SEP = re.compile(r"[\x01-\x08]")
_WS = re.compile(r"\s+")
_JO = re.compile(r"제\s*(\d+)\s*조")
# ⚠ **외부 법령 인용을 조 번호로 세면 안 된다.** R10 의 "누락 3조"(제6·38·39조)를 열어보니
# 전부 다른 법 인용이었다 — 「보험업법시행령」 제6조의2, 「신용정보의 이용 및 보호에 관한
# 법률」 제38조. 문서 자신의 구조 표지가 아니라 **본문 내용**이라, 이게 빠진 건
# anchor(구조) 문제가 아니라 char_coverage(내용) 문제다. 섞으면 지표가 뭘 재는지 흐려진다.
#   ① 「…법…」 다음에 오는 제N조   ② 제N조의M (조의 하위 항목 표기 = 인용에서 흔함)
_JO_CITATION = re.compile(
    r"[「｢][^」｣]{2,40}[」｣]\s*제\s*\d+\s*조"      # 법령명 뒤
    r"|제\s*\d+\s*조\s*의\s*\d+"                  # 제N조의M
)


def norm(s: str) -> str:
    """구분자 제어문자를 공백으로 바꾸고 공백을 없앤다 — 두 파서의 표기 차이를 흡수."""
    return _WS.sub("", _SEP.sub(" ", s or ""))


def unique_chars(pdf_text: str) -> int:
    """페이지 안에서 **같은 줄이 여러 번 나오면 한 번만** 센 글자 수.

    ⚠ 이걸 안 하면 char_coverage 가 파싱 품질이 아니라 **조판 장식**을 잰다.
    실측: KB 약관은 세로 탭/머리글이 한 페이지에 3~4번 렌더링되고 PyMuPDF 는 그걸 다 뽑는다
    ("제조준용규정" 260회 · "제조보상하는손해" 238회 — 조 번호가 별도 런이라 떨어져 나온다).
    ODL 은 이걸 올바르게 걷어내는데, 원시 글자 수로 비교하면 **잘 걷어낸 문서가 벌점**을 받는다.
        원본 cov   중복제거 후
        R05        1.000  →  1.000     (누락 0자, 애초에 중복 없음)
        R07        0.996  →  1.002
        SCALE_21084 0.896 →  1.068     ← 11%p 차이가 전부 이것 때문이었다
        SCALE_21088 0.886 →  1.062
    1.0 을 넘는 건 ODL 이 PyMuPDF 의 줄 분할과 다르게 이어붙이기 때문 — 손실이 아니다.
    """
    seen: set[str] = set()
    total = 0
    for line in (pdf_text or "").splitlines():
        nl = norm(line)
        if nl and nl not in seen:
            seen.add(nl)
            total += len(nl)
    return total


def _strip_citations(s: str) -> str:
    """외부 법령 인용 구간을 지운 텍스트. 구조 표지만 남긴다(위 _JO_CITATION 주석).

    ⚠ **정규화를 먼저 해야 한다.** 이 코퍼스는 법령명 안에도 \x01 구분자가 들어간다
    ("「신용정보의\x01 이용\x01 및\x01 보호에\x01 관한\x01 법률」제35조"). 원문 그대로
    정규식을 대면 PDF 쪽에서만 매칭되고 ODL 쪽에서는 안 돼서, **양쪽을 비대칭으로 지운다**.
    그러면 멀쩡한 인용이 '누락된 조'로 둔갑한다 — 실제로 R11 이 그렇게 13조 누락으로
    잡혔고, 열어보니 셋 다 ODL 에 멀쩡히 있는 인용이었다.
    """
    t = _SEP.sub(" ", s or "")
    t = re.sub(r"[ \t]+", " ", t)
    return _JO_CITATION.sub(" ", t)


def jo_set(s: str) -> set[int]:
    return {int(m.group(1)) for m in _JO.finditer(_strip_citations(s))}


def jo_seq(s: str) -> list[int]:
    return [int(m.group(1)) for m in _JO.finditer(_strip_citations(s))]


def page_metrics(doc_id: str, raw_json, pdf_path: pathlib.Path) -> tuple[list[dict], dict]:
    import pdfplumber
    import pymupdf

    from v1.utils.triage import TEXTY, flatten_odl

    nodes = flatten_odl(raw_json)
    by_page: dict[int, list] = collections.defaultdict(list)
    for n in nodes:
        if n["pg"] is not None:
            by_page[n["pg"]].append(n)

    rows: list[dict] = []
    odl_all_seq: list[int] = []
    with pymupdf.open(pdf_path) as mu, pdfplumber.open(pdf_path) as pl:
        for i in range(len(mu)):
            pg = i + 1
            ns = by_page.get(pg, [])
            odl_text = "\n".join(n["c"] for n in ns if n["t"] in TEXTY and n["c"].strip())
            pdf_text = mu[i].get_text("text")
            o_n = len(norm(odl_text))
            p_n = unique_chars(pdf_text)          # 페이지 내 중복 줄 1회만 (위 주석)
            p_raw = len(norm(pdf_text))
            try:
                plumb_tables = len(pl.pages[i].find_tables())
            except Exception:
                plumb_tables = None
            odl_all_seq += jo_seq(odl_text)
            rows.append({
                "page": pg,
                "odl_chars": o_n, "pdf_chars": p_n, "pdf_chars_raw": p_raw,
                # 중복이 많다 = 조판 장식이 많다. 진단용으로 남긴다(게이트 대상 아님)
                "dup_ratio": (round(1 - p_n / p_raw, 4) if p_raw else 0.0),
                # 원문이 비어 있으면(스캔) 커버리지는 정의되지 않는다 — 0 이 아니라 None
                "char_coverage": (round(o_n / p_n, 4) if p_n >= 50 else None),
                "odl_jo": sorted(jo_set(odl_text)), "pdf_jo": sorted(jo_set(pdf_text)),
                "odl_tables": sum(1 for n in ns if n["t"] == "table"),
                "plumb_tables": plumb_tables,
            })

    covs = [r["char_coverage"] for r in rows if r["char_coverage"] is not None]
    dens = [r["pdf_chars"] for r in rows if r["pdf_chars"] >= 50]
    med_den = st.median(dens) if dens else 0
    sparse = [r["page"] for r in rows
              if r["pdf_chars"] >= 50 and r["odl_chars"] < med_den * 0.10]

    pdf_jo_all: set[int] = set()
    odl_jo_all: set[int] = set()
    for r in rows:
        pdf_jo_all |= set(r["pdf_jo"])
        odl_jo_all |= set(r["odl_jo"])
    missing = sorted(pdf_jo_all - odl_jo_all)

    asc = sum(1 for a, b in zip(odl_all_seq, odl_all_seq[1:]) if b >= a)
    tdiff = [abs(r["odl_tables"] - r["plumb_tables"])
             for r in rows if r["plumb_tables"] is not None]

    summary = {
        "doc": doc_id, "pages": len(rows),
        "char_cov_median": round(st.median(covs), 4) if covs else None,
        "char_cov_min": round(min(covs), 4) if covs else None,
        "char_cov_p10": round(sorted(covs)[max(0, int(len(covs) * 0.10) - 1)], 4) if covs else None,
        "anchor_coverage": (round(len(odl_jo_all & pdf_jo_all) / len(pdf_jo_all), 4)
                            if pdf_jo_all else None),
        "anchor_missing": missing[:20], "anchor_missing_n": len(missing),
        "article_order": (round(asc / (len(odl_all_seq) - 1), 4) if len(odl_all_seq) > 1 else None),
        "table_diff_median": st.median(tdiff) if tdiff else None,
        "table_diff_max": max(tdiff) if tdiff else None,
        "sparse_pages": sparse[:20], "sparse_n": len(sparse),
    }
    return rows, summary


def _verdict(s: dict) -> tuple[str, list[str]]:
    """문서 하나의 판정. 사유를 함께 돌려준다 — 숫자만 보고는 왜 걸렸는지 모른다."""
    t, why = THRESHOLDS, []
    if s["anchor_missing_n"] > t["anchor_missing_max"]:
        why.append(f"조 누락 {s['anchor_missing_n']} ({s['anchor_missing'][:5]})")
    if s["char_cov_median"] is not None and s["char_cov_median"] < t["char_cov_median_min"]:
        why.append(f"글자 커버리지 중앙 {s['char_cov_median']} < {t['char_cov_median_min']}")
    if why:
        return "FAIL", why
    if s["char_cov_min"] is not None and s["char_cov_min"] < t["char_cov_min_warn"]:
        return "WARN", [f"최저 페이지 커버리지 {s['char_cov_min']} < {t['char_cov_min_warn']}"]
    return "PASS", []


BASELINE = ROOT / "data" / "eval" / "parse_baseline.json"


def _gate(out: list[dict], update_baseline: bool = False) -> int:
    """임계 적용 + tb_eval_run 저장. **기준선 대비 회귀**면 exit 1.

    ⚠ 절대 판정으로 차단하면 안 된다. 지금 코퍼스엔 R10 하나가 FAIL 인데, 열어보니 누락의
    대부분이 목차 점선·세로 탭·머리글 같은 **조판 장식**이다(본문도 일부 섞여 있어 조사 대상).
    이걸 이유로 게이트를 영구 적색으로 두면 사람이 습관적으로 무시하게 되고, 그건 게이트가
    없는 것보다 나쁘다 — 파괴적 명령 훅에서 `docker compose down` 을 뺀 것과 같은 판단.
    이 프로젝트의 다른 게이트(retrieval_baseline·routing_semantic_baseline)와 같은 규율로,
    **알려진 상태를 기준선에 고정하고 그보다 나빠질 때만** 막는다.
    """
    import subprocess
    import uuid

    from sqlalchemy import text

    from v1.config import task_session

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    items = []
    for s in out:
        v, why = _verdict(s)
        counts[v] += 1
        items.append((s["doc"], v, why, s))
        icon = {"PASS": "✅", "WARN": "⚠", "FAIL": "❌"}[v]
        note = ("  ← " + " · ".join(why)) if why else ""
        print(f"  {icon} {s['doc']:<14}cov중앙 {s['char_cov_median']} · 최저 {s['char_cov_min']} · "
              f"조누락 {s['anchor_missing_n']}{note}")

    # 컨테이너 안에는 git 이 없다 → Makefile 이 GIT_SHA 로 넘긴다. 없으면 None 으로 두되
    # **추측하지 않는다** — 어느 코드로 잰 값인지 모르면 두 run 의 차이를 해석할 수 없다.
    sha = os.environ.get("GIT_SHA") or None
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                 text=True, cwd=ROOT).stdout.strip() or None
        except FileNotFoundError:
            sha = None
    run_id = f"parse-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    summary = {"docs": len(out), **counts,
               "char_cov_median_min": min((s["char_cov_median"] for s in out
                                           if s["char_cov_median"] is not None), default=None),
               "anchor_missing_total": sum(s["anchor_missing_n"] for s in out)}
    with task_session() as db:
        db.execute(text("INSERT INTO tb_eval_run (run_id, kind, git_sha, config_json, summary_json) "
                        "VALUES (:r,'parse',:g,CAST(:c AS JSONB),CAST(:s AS JSONB))"),
                   {"r": run_id, "g": sha,
                    "c": json.dumps(THRESHOLDS), "s": json.dumps(summary, ensure_ascii=False)})
        for doc, v, why, s in items:
            db.execute(text("INSERT INTO tb_eval_item (run_id, item_id, scores_json, detail_json) "
                            "VALUES (:r,:i,CAST(:sc AS JSONB),CAST(:d AS JSONB))"),
                       {"r": run_id, "i": doc,
                        "sc": json.dumps({k: s[k] for k in
                                          ("char_cov_median", "char_cov_min", "anchor_coverage",
                                           "article_order", "table_diff_max", "sparse_n")}),
                        "d": json.dumps({"verdict": v, "why": why,
                                         "anchor_missing": s["anchor_missing"],
                                         "sparse_pages": s["sparse_pages"]}, ensure_ascii=False)})
        db.commit()

    print(f"\n  run {run_id} (git {sha}) 저장")
    print(f"  PASS {counts['PASS']} · WARN {counts['WARN']} · FAIL {counts['FAIL']}")

    cur = {doc: v for doc, v, _why, _s in items}
    if update_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"measured": time.strftime("%Y-%m-%d"), "git_sha": sha,
             "thresholds": THRESHOLDS, "verdicts": cur,
             "note": "알려진 상태. 이보다 나빠지면 회귀. R10 은 조판 장식(목차 점선·세로 탭·"
                     "머리글) 비중이 커서 FAIL 로 고정 — 본문 손실 여부는 V2 에서 조사."},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  기준선 고정 → {BASELINE.name}")
        return 0

    if not BASELINE.is_file():
        print("  ⚠ 기준선 없음 — `--update-baseline` 으로 먼저 고정할 것(차단하지 않음)")
        return 0

    base = json.loads(BASELINE.read_text(encoding="utf-8"))["verdicts"]
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    worse = [(d, base.get(d, "PASS"), v) for d, v in cur.items()
             if rank[v] > rank.get(base.get(d, "PASS"), 0)]
    new_doc = [d for d in cur if d not in base]
    if new_doc:
        print(f"  ℹ 기준선에 없는 신규 문서 {len(new_doc)}건: {', '.join(new_doc[:5])}")
    if worse:
        for d, b, v in worse:
            print(f"  ❌ 회귀 {d}: {b} → {v}")
        print("  → 배포 차단 (exit 1)")
        return 1
    print("  ✅ 기준선 대비 회귀 없음")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--distribution", action="store_true")
    ap.add_argument("--doc")
    ap.add_argument("--out", default="data/eval/parse_quality.json")
    ap.add_argument("--gate", action="store_true", help="임계 적용 + tb_eval_run 저장, 회귀 시 exit 1")
    ap.add_argument("--update-baseline", action="store_true", help="현재 판정을 기준선으로 고정")
    a = ap.parse_args()

    from sqlalchemy import text

    from v1.config import task_session
    from v1.utils.source import resolve_source

    with task_session() as db:
        q = ("SELECT document_id, document_name, raw_json FROM tb_document_extract "
             "WHERE raw_json IS NOT NULL")
        params = {}
        if a.doc:
            q += " AND document_id = :d"
            params["d"] = a.doc
        rows = db.execute(text(q + " ORDER BY document_id"), params).all()

    targets = [(d, n, rj) for d, n, rj in rows if d not in BENCH_DOCS]
    print(f"  대상 {len(targets)}문서 (벤치 {len(rows) - len(targets)}건 제외)", flush=True)

    out, t0 = [], time.time()
    for did, name, rj in targets:
        src, _stage = resolve_source(name)
        if not src:
            print(f"  ⚠ 원본 없음 {did}", flush=True)
            continue
        try:
            _pages, s = page_metrics(did, rj, src)
        except Exception as e:
            print(f"  ⚠ {did}: {type(e).__name__}: {e}", flush=True)
            continue
        out.append(s)
        print(f"    {did:<14}{s['pages']:>4}p  cov중앙 {s['char_cov_median']}  "
              f"cov최소 {s['char_cov_min']}  앵커 {s['anchor_coverage']}  "
              f"누락 {s['anchor_missing_n']}  순서 {s['article_order']}  "
              f"표차 중앙 {s['table_diff_median']}/최대 {s['table_diff_max']}  "
              f"희소 {s['sparse_n']}", flush=True)

    p = ROOT / a.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  {time.time() - t0:.0f}초 · → {p}", flush=True)

    if a.gate or a.update_baseline:
        return _gate(out, update_baseline=a.update_baseline)

    if a.distribution and out:
        print("\n── 코퍼스 분포 (임계 정하기 전에 보는 것) ──")
        for key, label in [("char_cov_median", "char_coverage 문서중앙"),
                           ("char_cov_min", "char_coverage 문서최소"),
                           ("anchor_coverage", "anchor_coverage"),
                           ("article_order", "article_order")]:
            v = sorted(x[key] for x in out if x[key] is not None)
            if not v:
                continue
            print(f"  {label:<26} min {v[0]:.3f} · p25 {v[len(v)//4]:.3f} · "
                  f"중앙 {v[len(v)//2]:.3f} · max {v[-1]:.3f}")
        miss = sorted(x["anchor_missing_n"] for x in out)
        sp = sorted(x["sparse_n"] for x in out)
        td = sorted(x["table_diff_max"] for x in out if x["table_diff_max"] is not None)
        print(f"  {'anchor 누락 조 수':<26} min {miss[0]} · 중앙 {miss[len(miss)//2]} · max {miss[-1]}"
              f"  (0 인 문서 {sum(1 for x in miss if x == 0)}/{len(miss)})")
        print(f"  {'sparse 페이지 수':<26} min {sp[0]} · 중앙 {sp[len(sp)//2]} · max {sp[-1]}"
              f"  (0 인 문서 {sum(1 for x in sp if x == 0)}/{len(sp)})")
        if td:
            print(f"  {'table_diff 최대':<26} min {td[0]} · 중앙 {td[len(td)//2]} · max {td[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
