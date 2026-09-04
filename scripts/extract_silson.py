"""실손 자기부담금·공제 추출 — 소비자 "실제로 내가 얼마 내나"의 결정론 값.

실손은 지급액이 '보장대상의료비 − 자기부담금' 이라, 지급률(payout_rule)만으로는 답이 안 된다.
자기부담 체계가 곧 상품 비교축이다: 급여 20% / 비급여 30% / 비급여 3대(도수·주사·MRI) 별도 공제.

⚠ **세대(generation)를 먼저 판정한다.** 실손은 규제 세대마다 자기부담 체계 자체가 다르다:
    3세대(2017.04~2021.06)  급여/비급여 통합 체계 · 특약 분리
    4세대(2021.07~)         급여 20% / 비급여 30% 분리 + 비급여 3대 별도 공제
    5세대(2026~)            비급여를 **중증/비중증**으로 재분할 (비중증 50%)
세대를 섞어 "회사 A는 20%, 회사 B는 10%" 로 비교하면 **회사 차이가 아니라 세대 차이**를
회사 차이로 오독한다. 그래서 4세대로 확정되지 않으면 값을 내지 않는다(generation=None).

실측(2026-09-04) — 수집된 5개사가 세대 혼합이었다:
    삼성화재 2501 · DB다이렉트 2301                      → 4세대
    삼성다이렉트 2605                                    → 5세대(비중증 50%)
    DB손보 2101(2021.01) · 현대해상 Hi1904(2019.04)      → 4세대 이전
`corpus-provenance.md` 는 범위를 "4세대(2021.7~)" 로 못박았으나 실제 수집분은 3~5세대가
섞여 있었다. 5개사 자기부담률을 나란히 놓는 비교표는 지금 데이터로는 만들 수 없다.

표기 두 형식을 모두 흡수한다(도메인 §1 "구조가 아니라 포맷이 다르다"):
    표   삼성: |담보명|자기부담금 차감금액| → "입원치료 : 보장대상의료비의 20%"
    평문 DB다이렉트: "공제금액(1~2만원)과 보장대상의료비의20%중 큰금액"

용법:
    python3 scripts/extract_silson.py            # 전 문서 추출 + 골든 채점
    python3 scripts/extract_silson.py <문서명>    # 한 문서만
"""
import re
import sys
import json
import pathlib

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "output" / "raw"
GOLDEN = DATA / "eval" / "golden_silson.jsonl"

# 4세대 판정 — 급여/비급여를 **다른 비율로** 나누는 게 4세대의 정의적 특징이다.
# 숫자만 보면 3세대 문서의 다른 맥락 20%/30%에 걸리므로 '보장대상의료비' 앵커를 함께 요구한다.
_R20 = re.compile(r'보장\s*대상\s*의료비\s*의\s*20\s*%')
_R30 = re.compile(r'보장\s*대상\s*의료비\s*의\s*30\s*%')
_R50 = re.compile(r'보장\s*대상\s*의료비\s*의\s*50\s*%')   # 5세대 비중증 비급여


def _near(text: str, rx, label: str, window: int = 45) -> bool:
    """비율 표기 **앞뒤** window자 안에 급여/비급여 라벨이 있나.

    Why 양방향: DB다이렉트는 레이아웃 복원 결과 값이 라벨보다 먼저 온다
    ("…20%중 큰금액" 다음 줄에 "상해급여 질병급여"). 단방향이면 4세대인데 놓친다.
    '비급여' 안에 '급여'가 들어 있어, 급여를 찾을 땐 '비급여'를 지운 뒤 본다.
    """
    for m in rx.finditer(text):
        w = text[max(0, m.start() - window): m.end() + window]
        if label == "급여":
            w = w.replace("비급여", "")
        if label in w:
            return True
    return False
# 비급여 3대(도수치료·주사료·MRI) 공제: "3만원과 보장대상의료비의 30%중 큰 금액"
_3DAE = re.compile(r'(\d{1,3})\s*만\s*원[^\n]{0,12}?보장\s*대상\s*의료비\s*의\s*30\s*%')
# 급여 통원 공제: "공제금액(1~2만원)과 …20%" — 병원급별이라 범위로 표기된다
_OUT  = re.compile(r'공제\s*금액\s*\(\s*(\d)\s*[~∼-]\s*(\d)\s*만\s*원\s*\)')
# 상급병실료 자기부담: "상급병실이용 : 비급여 병실료의 50%"
_ROOM = re.compile(r'비급여\s*병실료\s*의\s*(\d{1,2})\s*%')


def _norm(md: str) -> str:
    """표 마크업·개행을 지워 표/평문 두 형식을 같은 축으로 본다."""
    return re.sub(r'\s+', ' ', md.replace("<br>", " ").replace("|", " "))


def extract_silson(doc: str) -> dict:
    p = RAW / f"{doc}.md"
    out = {"doc": doc, "generation": None, "급여_자기부담률": None,
           "비급여_자기부담률": None, "비급여3대_공제액": None,
           "급여통원_공제액_최소": None, "급여통원_공제액_최대": None,
           "상급병실_자기부담률": None, "note": None}
    if not p.exists():
        out["note"] = "원문 없음"
        return out
    t = _norm(p.read_text(encoding="utf-8", errors="replace"))

    if not (_near(t, _R20, "급여") and _near(t, _R30, "비급여")):
        # 급여20·비급여30 분리가 확인 안 되면 4세대로 단정하지 않는다 → 값 없음(precision-first)
        out["note"] = "4세대 미확정 — 급여20/비급여30 분리 미검출. 세대별 체계가 달라 값 미산출"
        return out

    # 5세대는 비급여를 **중증/비중증으로 갈라** 비중증에 50%를 매긴다. 이걸 4세대로 보고
    # "비급여 30%" 한 값만 내면 비중증 50%를 숨긴 오답이 된다(실측: 삼성다이렉트 2605 —
    # 비중증 11회·보장대상의료비의 50% 3회. 4세대 문서 둘은 각각 0회로 깨끗이 갈린다).
    if _R50.search(t) and "비중증" in t:
        out["generation"] = 5
        out["note"] = ("5세대(중증/비중증 분리) — 비급여가 중증 30%·비중증 50%로 갈려 "
                       "단일 비급여율로 요약 불가. 4세대와 나란히 비교하면 안 됨")
        return out

    out["generation"] = 4
    out["급여_자기부담률"] = 20
    out["비급여_자기부담률"] = 30
    m = _3DAE.search(t)
    if m:
        out["비급여3대_공제액"] = int(m.group(1)) * 10000
    m = _OUT.search(t)
    if m:
        out["급여통원_공제액_최소"] = int(m.group(1)) * 10000
        out["급여통원_공제액_최대"] = int(m.group(2)) * 10000
    m = _ROOM.search(t)
    if m:
        out["상급병실_자기부담률"] = int(m.group(1))
    return out


DOCS = [
    "삼성화재_실손의료비보험_2501",
    "삼성다이렉트_실손의료비보험_2605",
    "DB다이렉트_참좋은종합보험_2301",
    "DB손보_프로미라이프실손의료비보험_2101",
    "현대해상_실손의료비보장보험_Hi1904",
]


def _score():
    """골든 채점 — 필드별 TP/FP/FN. 4세대 아닌 문서의 None 은 정답(TN)이다."""
    if not GOLDEN.exists():
        print(f"골든 없음: {GOLDEN}", file=sys.stderr)
        return 1
    gold = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            gold.setdefault(r["doc"], {})[r["field"]] = r["expected"]

    tp = fp = fn = tn = 0
    for doc, fields in gold.items():
        got = extract_silson(doc)
        for f, exp in fields.items():
            act = got.get(f)
            if exp is None and act is None:
                tn += 1
            elif exp is None and act is not None:
                fp += 1
                print(f"  FP {doc} {f}: 정답 None ← {act}")
            elif act is None:
                fn += 1
                print(f"  FN {doc} {f}: 정답 {exp} ← None")
            elif act == exp:
                tp += 1
            else:
                fp += 1
                print(f"  FP {doc} {f}: 정답 {exp} ← {act}")
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    print(f"  실손 자기부담  recall={rec:.2f} precision={prec:.2f} "
          f"(TP{tp} FN{fn} FP{fp} TN{tn})")
    return 0 if (fp == 0 and fn == 0) else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "--score":
        print(json.dumps(extract_silson(sys.argv[1]), ensure_ascii=False, indent=2))
        return
    if "--score" in sys.argv:
        sys.exit(_score())
    for d in DOCS:
        r = extract_silson(d)
        g = r["generation"]
        if g:
            print(f"  {d[:34]:<36} {g}세대  급여{r['급여_자기부담률']}% "
                  f"비급여{r['비급여_자기부담률']}%  3대공제 {r['비급여3대_공제액']} "
                  f"통원공제 {r['급여통원_공제액_최소']}~{r['급여통원_공제액_최대']} "
                  f"상급병실 {r['상급병실_자기부담률']}%")
        else:
            print(f"  {d[:34]:<36} —      {r['note']}")


if __name__ == "__main__":
    main()
