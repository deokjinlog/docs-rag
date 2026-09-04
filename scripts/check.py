"""CI 게이트 — 전 골든셋 + 전처리 게이트를 한 번에 돌려 회귀를 자동 차단(로드맵 C7).

배포·커밋 관문. 하나라도 실패(❌)면 exit 1 → CI가 머지/배포를 막는다. 스택 없이 도는
자립 검증(관계형 추출 계층). 실 vLLM/DB 서빙 게이트는 별도(smoke_test·eval_ragas, 스택 필요).

용법: python3 scripts/check.py      (또는 make check)
"""
import os
import sys
import subprocess

HERE = os.path.dirname(__file__)

# (이름, [스크립트, 인자...]) — 각 스크립트는 실패 행에 ❌를 찍는다(공통 규약).
CHECKS = [
    ("parse",        ["parse_golden.py"]),          # 파싱 골든(조 수·제목 정답 대조)
    ("payout",       ["extract_payout.py"]),
    ("payout_qa",    ["query_payout.py"]),
    ("terms",        ["extract_terms.py"]),
    ("coverage",     ["judge_coverage.py"]),
    ("kb_coverage",  ["extract_kb_coverage.py"]),   # KB 암 별표3(유사암 제외 범위 뺄셈)
    ("exclusion",    ["extract_exclusion_reasons.py"]),
    ("catalog",      ["extract_coverages.py"]),
    ("waiting",      ["extract_waiting.py"]),        # KB 면책기간·감액(담보별)
    ("silson",       ["extract_silson.py", "--score"]),  # 실손 자기부담·공제(세대 게이트)
    ("completeness", ["assemble_answer.py"]),
    ("reconcile",    ["assemble_answer.py", "--reconcile"]),
    ("gate",         ["gate.py"]),                 # FAIL 있으면 ❌(WARN ⚠는 통과)
]


def main():
    print("CI 게이트 — 관계형 추출 계층 자립 검증")
    print("=" * 70)
    fails = []
    for name, args in CHECKS:
        r = subprocess.run([sys.executable, os.path.join(HERE, args[0])] + args[1:],
                           capture_output=True, text=True)
        failed = ("❌" in r.stdout) or (r.returncode != 0)   # 실패행 ❌(stdout) or 크래시
        tail = next((l for l in reversed(r.stdout.strip().splitlines()) if l.strip()), "")
        print(f"  {'❌' if failed else '✅'} {name:<14} {tail.strip()[:58]}")
        if failed:
            fails.append(name)
    print("-" * 70)
    if fails:
        print(f"❌ 회귀 감지: {fails} → 배포 차단 (exit 1)")
        sys.exit(1)
    print(f"✅ 전 골든 {len(CHECKS)}종 통과 → 배포 게이트 OK (exit 0)")


if __name__ == "__main__":
    main()
