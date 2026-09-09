"""프로필별 mem_limit 합계 검증 (D1).

**왜 필요한가** — WSL 15Gi 안에서 전부 띄운 채로 인제스트를 돌리다 스택이 **4회** 통째로
죽었다. 4회차는 커널 OOM 기록이 없고 postgres 가 exit 0 이었다: cgroup 한계가 아니라
Docker Desktop/WSL 레벨에서 내려갔다는 뜻이라, 한도를 **높이는** 쪽은 답이 아니다.
동시에 뜨는 조합을 줄이고, 그 합계를 기계가 지키게 한다.

한도가 없는 서비스는 **실패로 본다** — 천장 없는 컨테이너 하나가 WSL 전체를 끌고 간
전례가 있다(api 누수, CLAUDE.md). 실제로 postgres·qdrant·rabbitmq 가 무제한이었다.

용법:  python3 scripts/mem_budget.py            # 전 프로필 검증 (초과 시 exit 1)
       python3 scripts/mem_budget.py --limit 11
"""
import argparse
import subprocess
import sys

PROFILES = ["serve", "ingest", "ocr", "inspect"]
GIB = 1024 ** 3


def profile_services(profile: str, extra: list[str]) -> dict[str, int | None]:
    cmd = ["docker", "compose"]
    for f in extra:
        cmd += ["-f", f]
    cmd += ["--profile", profile, "config"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        print(f"  ✗ {profile}: compose config 실패\n{out.stderr[:300]}", file=sys.stderr)
        return {}
    svc, cur = {}, None
    for line in out.stdout.splitlines():
        if line.startswith("  ") and line.endswith(":") and not line.startswith("    "):
            cur = line.strip().rstrip(":")
            if cur not in ("networks", "volumes", "services", "default"):
                svc.setdefault(cur, None)
        elif cur and line.strip().startswith("mem_limit:"):
            svc[cur] = int(line.split(":", 1)[1].strip().strip('"'))
    # config 는 volumes/networks 섹션도 같은 들여쓰기로 뱉는다 — 한도도 이름도 없는 건 뺀다
    return {k: v for k, v in svc.items() if v is not None or k in _known(out.stdout)}


def _known(cfg: str) -> set[str]:
    """services: 블록 아래 이름만 서비스로 인정."""
    names, inside = set(), False
    for line in cfg.splitlines():
        if line.startswith("services:"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            inside = False
        if inside and line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            names.add(line.strip().rstrip(":"))
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=float, default=11.0, help="프로필당 상한(GiB)")
    ap.add_argument("-f", dest="files", action="append", default=[], help="추가 compose 파일")
    a = ap.parse_args()

    bad = 0
    print(f"  {'프로필':<10}{'합계':>9}{'상한':>8}   구성")
    print("  " + "─" * 74)
    for prof in PROFILES:
        svc = profile_services(prof, a.files)
        if not svc:
            bad += 1
            continue
        missing = [k for k, v in svc.items() if v is None]
        total = sum(v for v in svc.values() if v) / GIB
        parts = " + ".join(f"{k} {v / GIB:g}" for k, v in sorted(svc.items()) if v)
        ok = total <= a.limit and not missing
        mark = "✅" if ok else "❌"
        print(f"  {prof:<10}{total:>7.1f}Gi{a.limit:>7.0f}Gi {mark} {parts}")
        if missing:
            print(f"  {'':10}{'':16}   ⚠ 한도 미설정: {', '.join(missing)}")
        if not ok:
            bad += 1
    print("  " + "─" * 74)
    if bad:
        print(f"  ❌ {bad}개 프로필이 예산을 어긴다 — 동시에 뜨는 구성을 줄이거나 한도를 실측으로 조일 것")
        return 1
    print("  ✅ 전 프로필 예산 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
