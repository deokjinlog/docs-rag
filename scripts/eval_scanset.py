"""합성 스캔셋 채점 — OCR vs VL 을 **CER(문자 오류율)** 로 잰다. (캐스케이드 M3)

정답은 `make_scanset.py` 가 네이티브 텍스트 레이어에서 뽑아둔 것이다. 같은 페이지를
이미지로 구워 다시 읽게 하고, 원문과 얼마나 다른지를 문자 단위로 센다.

    CER = 편집거리(예측, 정답) / len(정답)      0 이 완벽, 1 이면 전부 틀림

**왜 CER 인가** — "몇 자 뽑았나"(chars)로는 품질을 못 잰다. 실측에서 OCR 이 요약서 표지를
`'쉽게극웨위o Y F T-2'` 로 44자 뽑았는데, 글자 수만 보면 "뽑긴 뽑았다"가 된다.
CER 은 그게 쓰레기임을 숫자로 만든다.

**정규화**: 공백·줄바꿈을 하나로 접고 비교한다. 레이아웃 차이(줄바꿈 위치)는 OCR 품질이
아니라 재구성 소관이라 여기서 벌점을 주면 축이 섞인다. 구조는 별도 지표(조 순서)로 잰다.

용법:
    # OCR (paddle 컨테이너, GPU 필요 — CPU 는 oneDNN 버그로 죽는다)
    python3 scripts/eval_scanset.py --engine ocr
    python3 scripts/eval_scanset.py --engine vl
    python3 scripts/eval_scanset.py --report          # 저장된 결과만 표로
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANSET = ROOT / "data/eval/scanset"

_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    """공백 접기 — 줄바꿈 위치 차이로 CER 이 오염되는 걸 막는다."""
    return _WS.sub(" ", (s or "").strip())


def cer(pred: str, truth: str) -> float:
    """문자 오류율. rapidfuzz 가 있으면 그걸 쓰고(빠름), 없으면 표준 DP."""
    p, t = norm(pred), norm(truth)
    if not t:
        return 0.0 if not p else 1.0
    try:
        from rapidfuzz.distance import Levenshtein
        return Levenshtein.distance(p, t) / len(t)
    except ImportError:
        pass
    # DP 폴백 — O(len(p)*len(t)) 라 긴 문서면 느리다
    prev = list(range(len(t) + 1))
    for i, ch in enumerate(p, 1):
        cur = [i]
        for j, tc in enumerate(t, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ch != tc)))
        prev = cur
    return prev[-1] / len(t)


def line_recall(pred_lines: list[str], truth: str, thr: float = 0.7) -> tuple[float, float]:
    """**순서 독립** 인식 품질 — 정답 줄이 예측 어딘가에 있나.

    CER 만 보면 안 되는 이유: 정답은 `page.get_text("text")` 가 만든 **PyMuPDF 순서**다.
    OCR 이 글자를 완벽히 읽어도 줄 순서가 다르면 편집거리가 치솟는다. 실측에서 문자 겹침이
    77.2% 인데 CER 0.75 였고, bbox 재정렬로도 1.5%p 밖에 안 줄었다 — 남은 건 인식 오류가
    아니라 **두 순서의 차이**다. 그래서 인식 품질은 순서를 빼고 재야 한다.

    반환 (recall, 평균유사도): 정답 줄 중 임계 이상으로 매칭된 비율과, 그 매칭들의 평균.
    """
    tl = [l.strip() for l in truth.splitlines() if len(l.strip()) >= 4]
    # VL 은 마크다운을 통짜로 준다(파일당 1개) — 줄로 펴야 OCR 과 같은 축에서 비교된다.
    # 안 펴면 거대한 '한 줄' 하나가 되어 매칭이 전부 실패하고 recall 이 0 으로 나온다(실측).
    pl = [ln.strip() for chunk in pred_lines for ln in str(chunk).splitlines() if ln.strip()]
    if not tl:
        return (0.0, 0.0)
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return (0.0, 0.0)
    hits, sims = 0, []
    for t in tl:
        m = process.extractOne(t, pl, scorer=fuzz.ratio)
        if m and m[1] / 100 >= thr:
            hits += 1
            sims.append(m[1] / 100)
    return (round(hits / len(tl), 4), round(sum(sims) / len(sims), 4) if sims else 0.0)


def trigram_recall(pred: str, truth: str) -> float:
    """**순서·줄구조 모두 독립**인 인식 품질 — 문자 3-gram 집합 겹침.

    왜 세 번째 지표가 필요한가 — 앞의 둘이 각각 한쪽에 유리하게 편향돼 있다:

        줄 recall   OCR 은 줄 단위로 뱉고 VL 은 문단으로 합친다 → **OCR 에 유리**
                    (실측: OCR 0.75 vs VL 0.24. VL 이 못 읽어서가 아니라 줄이 안 맞아서)
        CER         OCR 은 검출 순서라 뒤섞이고 VL 은 읽기순서를 지킨다 → **VL 에 유리**
                    (실측: OCR 0.76 vs VL 0.28)

    3-gram 은 순서도 줄바꿈도 안 본다. "글자를 얼마나 맞게 읽었나"만 남는다.
    """
    def grams(x: str) -> set[str]:
        x = _WS.sub("", x or "")
        return {x[i:i + 3] for i in range(len(x) - 2)}
    t = grams(truth)
    if not t:
        return 0.0
    return round(len(t & grams(pred)) / len(t), 4)


# ── 엔진 실행 (paddle 컨테이너 안에서) ────────────────────────────────────────
# GPU 가 필요하다. paddle 은 기본 CPU 고정이라 오버레이가 필요하고, CUDA compat 이
# WSL 드라이버를 가리는 문제도 우회해야 한다(docs/troubleshooting-boot.md).
_ENV = ("-e", "LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/x86_64-linux-gnu:"
                              "/usr/local/cuda-13.0/targets/x86_64-linux/lib",
        "-e", "CUDA_VISIBLE_DEVICES=0")

_RUNNER = r'''
import paddle, pathlib, json, time, sys
paddle.set_device("gpu:0")
engine = sys.argv[1]
items = json.loads(pathlib.Path("/data/eval/scanset/manifest.json").read_text())
if engine == "ocr":
    from paddleocr import PPStructureV3
    m = PPStructureV3(use_doc_orientation_classify=False, use_doc_unwarping=False, lang="korean")
    def run(p):
        txt, confs, polys = [], [], []
        for r in m.predict(p):
            o = (r.json.get("res") or {}).get("overall_ocr_res") or {}
            txt += o.get("rec_texts") or []
            confs += o.get("rec_scores") or []
            polys += [[[float(c) for c in pt] for pt in poly]
                      for poly in (o.get("dt_polys") or o.get("rec_polys") or [])]
        return txt, confs, polys
else:
    from paddleocr import PaddleOCRVL
    m = PaddleOCRVL()
    def run(p):
        out = pathlib.Path("/tmp/vlout"); out.mkdir(exist_ok=True)
        for f in out.glob("*.md"): f.unlink()
        for r in m.predict(p): r.save_to_markdown(str(out))
        return [f.read_text(errors="replace") for f in sorted(out.glob("*.md"))], [], []
res = []
for it in items:
    ip = "/data/eval/scanset/" + it["image"]
    t0 = time.time()
    try:
        text, confs, polys = run(ip)
        res.append({**it, "engine": engine, "lines": text, "confs": confs, "polys": polys,
                    "sec": round(time.time() - t0, 1)})
    except Exception as e:
        res.append({**it, "engine": engine, "error": f"{type(e).__name__}: {str(e)[:80]}",
                    "sec": round(time.time() - t0, 1)})
pathlib.Path(f"/data/eval/scanset/out_{engine}.json").write_text(
    json.dumps(res, ensure_ascii=False), encoding="utf-8")
'''


def run_engine(engine: str) -> int:
    cmd = ["docker", "compose", "exec", "-T", *_ENV, "paddle", "python3", "-c", _RUNNER, engine]
    print(f"  {engine} 실행 중… (GPU, 24장)")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=7200)
    out = SCANSET / f"out_{engine}.json"
    if not out.exists():
        print(f"  ❌ 결과 없음. stderr 끝:\n{r.stderr[-600:]}", file=sys.stderr)
        return 1
    print(f"  ✅ {out}")
    return 0


def _cer_reordered(r: dict, truth: str) -> float | None:
    """**M2 의 bbox 재정렬을 먹인 뒤** CER. 없으면 None.

    OCR 은 검출 순서로 줄을 뱉는다 — 다단 페이지면 단을 가로질러 뒤섞인다.
    실측(clean p25): 문자 겹침은 77.2% 인데 CER 0.76 이었다. 내용은 뽑았고 **순서만**
    틀린 것이다. 원시 CER 만 보면 "OCR 이 못 읽는다"로 오독한다.
    같은 재정렬이 ODL 출력의 읽기순서를 고쳤으니(M2, 바이트 동일 검증) 여기도 먹여본다 —
    이게 공통 스키마를 만든 목적 그 자체다.
    """
    polys, lines = r.get("polys") or [], r.get("lines") or []
    if not polys or len(polys) != len(lines):
        return None          # 짝이 안 맞으면 재정렬 불가 — 근사로 때우지 않는다
    sys.path.insert(0, str(ROOT / "scripts"))
    from blocks import from_ocr, reorder, to_markdown
    res = {"overall_ocr_res": {"rec_texts": lines, "rec_scores": r.get("confs") or [],
                               "dt_polys": polys}}
    # 이미지 폭을 넘겨 픽셀 → PDF 포인트로 정규화한다. 안 넘기면 COL_GAP 이 안 맞아
    # 단이 과하게 쪼개진다(실측: 2단 페이지에서 경계 7개).
    iw = (r.get("size") or [None])[0]
    blocks = reorder(from_ocr(res, page=r["page"], image_width=iw))
    return round(cer(to_markdown(blocks), truth), 4)


def report() -> int:
    truths = {}
    rows = []
    for engine in ("ocr", "vl"):
        p = SCANSET / f"out_{engine}.json"
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")):
            tp = SCANSET / r["truth"]
            truths.setdefault(r["truth"], tp.read_text(encoding="utf-8"))
            if r.get("error"):
                r["cer"] = r["cer_ro"] = None
            else:
                r["cer"] = round(cer(" ".join(r.get("lines") or []), truths[r["truth"]]), 4)
                r["cer_ro"] = _cer_reordered(r, truths[r["truth"]])
                r["lrec"], r["lsim"] = line_recall(r.get("lines") or [], truths[r["truth"]])
                r["tri"] = trigram_recall(" ".join(r.get("lines") or []), truths[r["truth"]])
            rows.append(r)
    if not rows:
        print("결과 없음 — --engine ocr / --engine vl 을 먼저 돌린다", file=sys.stderr)
        return 2

    import statistics
    print(f"  {'변형':<8}{'엔진':<6}{'n':>4}{'3-gram':>9}{'줄recall':>10}{'줄유사도':>10}"
          f"{'CER':>8}{'초/장':>8}")
    print("  " + "─" * 68)
    by = {}
    for r in rows:
        by.setdefault((r["variant"], r["engine"]), []).append(r)
    for variant in ("clean", "low", "skew", "dark"):
        best = {}
        for engine in ("ocr", "vl"):
            rs = by.get((variant, engine))
            if not rs:
                continue
            cs = [r["cer"] for r in rs if r["cer"] is not None]
            if not cs:
                print(f"  {variant:<8}{engine:<6}{len(rs):>4}{'전부 실패':>10}")
                continue
            best[engine] = statistics.median(cs)
            ro = [r["cer_ro"] for r in rs if r.get("cer_ro") is not None]
            rom = statistics.median(ro) if ro else None
            gain = f"{statistics.median(cs) - rom:+.3f}" if rom is not None else "—"
            lr = statistics.median([r.get("lrec", 0) for r in rs])
            ls = statistics.median([r.get("lsim", 0) for r in rs])
            tri = statistics.median([r.get("tri", 0) for r in rs])
            best[engine] = -tri          # 판정은 3-gram 으로 — 편향 없는 축
            print(f"  {variant:<8}{engine:<6}{len(cs):>4}{tri:>9.3f}{lr:>10.3f}{ls:>10.3f}"
                  f"{statistics.median(cs):>8.3f}{statistics.mean(r['sec'] for r in rs):>8.1f}")
        if len(best) == 2:
            w = min(best, key=best.get)
            gap = abs(best['ocr'] - best['vl'])
            print(f"  {'':<8}{'':<6}{'':>4}{'':>10}{'':>10}{'':>8}   "
                  f"→ {w.upper()} 우세 (차 {gap:.3f})")
    print("\n  3-gram   = 문자 3-gram 겹침(**순서·줄구조 독립**) — 판정 기준")
    print("  줄recall = 정답 줄이 예측에 있나 (줄 단위로 뱉는 OCR 에 유리)")
    print("  CER      = 편집거리/정답길이 (읽기순서를 지키는 VL 에 유리)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["ocr", "vl"])
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.engine:
        rc = run_engine(a.engine)
        if rc:
            return rc
    return report()


if __name__ == "__main__":
    sys.exit(main())
