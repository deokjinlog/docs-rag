"""문서 안의 **서로 같은 내용인 청크**를 묶는다 — 뷰어의 진단 렌즈.

**왜 필요한가.** 이 코퍼스의 약관은 같은 사실을 여러 곳에 적는다 — 가입자 유의사항 요약표에
한 번, 본문 조에 한 번, 그리고 특약마다 또 한 번. 검색 골든의 `dup@10` 이 그 대가를 숫자로
보여줬지만(top-10 의 27%가 같은 문장), **어디가 겹치는지**는 숫자로 안 보인다. 사람이 지울지
말지 판단하려면 겹치는 쌍을 눈으로 봐야 한다.

**`dup@10` 과 다른 렌즈다.** 저쪽은 *"이 질의에 같은 사실이 몇 번 회수되나"*(질의 기준),
여기는 *"이 문서 안에 서로 같은 청크가 어디 있나"*(문서 기준). 둘을 같은 수치로 읽으면 안 된다.

**임계를 게이트로 쓰지 않는다.** 기본값 0.6 은 분포를 보기 위한 출발점이고, 화면은 각 군의
실제 포함도를 같이 보여준다. 여기서 아무것도 차단하지 않으므로, 분포를 본 뒤에 임계를
정한다는 이 프로젝트의 규율과 충돌하지 않는다 — 오히려 그 분포를 보여주는 게 이 화면의 일이다.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

# 40자 창을 20자씩 밀며 뜬다. 창이 짧으면 조문 상투구("회사는 …합니다")가 전부 걸리고,
# 길면 표처럼 한 셀만 다른 중복을 놓친다. 20자 간격은 창의 절반 — 경계에서 잘려도 하나는 잡힌다.
SHINGLE_W = 40
SHINGLE_STEP = 20

# 한 shingle 이 이보다 많은 청크에 나타나면 **문서 공통 상투구**로 보고 후보 생성에서 뺀다.
# 안 빼면 "회사는 다음의 경우 보험금을 지급하지 않습니다" 하나로 수백 쌍이 후보가 되어
# O(n²) 가 터지고, 정작 의미 있는 중복은 그 잡음에 묻힌다.
BOILERPLATE_DF = 12

# 포함도(containment) = 공유 shingle / min(|A|,|B|). Jaccard 가 아니라 containment 인 이유:
# 요약표(짧다)가 본문(길다)에 통째로 들어가 있는 게 우리가 찾는 전형인데, Jaccard 는 길이
# 차이 때문에 그걸 낮게 매긴다.
DEFAULT_MIN_CONTAINMENT = 0.6

_NOISE = re.compile(r"[\s|]+")


def norm(s: str) -> str:
    """비교용 정규화 — 공백·표 구분자를 지운다.

    표는 같은 내용이라도 셀 폭에 따라 파이프 위치가 달라진다. 그걸 살려두면 같은 표의 두
    렌더링이 다른 문자열이 된다.
    """
    return _NOISE.sub("", unicodedata.normalize("NFKC", s or "")).lower()


def shingles(text: str) -> set[str]:
    n = norm(text)
    if len(n) < SHINGLE_W:
        return {n} if n else set()
    return {n[i:i + SHINGLE_W] for i in range(0, len(n) - SHINGLE_W + 1, SHINGLE_STEP)}


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def duplicate_groups(
    chunks: list[tuple[int, str]],
    min_containment: float = DEFAULT_MIN_CONTAINMENT,
) -> tuple[dict[int, int], dict[int, float]]:
    """`[(chunk_id, content)]` → `({chunk_id: group_no}, {group_no: 최저 포함도})`.

    group_no 는 1부터. 중복이 없는 청크는 매핑에 없다.
    """
    sh = {cid: shingles(c) for cid, c in chunks if c}
    index: dict[str, list[int]] = defaultdict(list)
    for cid, s in sh.items():
        for g in s:
            index[g].append(cid)

    cand: dict[tuple[int, int], int] = defaultdict(int)
    for g, ids in index.items():
        u = sorted(set(ids))
        if not (2 <= len(u) <= BOILERPLATE_DF):
            continue
        for i in range(len(u)):
            for j in range(i + 1, len(u)):
                cand[(u[i], u[j])] += 1

    uf, scores = _Union(), {}
    for (a, b), shared in cand.items():
        denom = min(len(sh[a]), len(sh[b])) or 1
        c = shared / denom
        if c >= min_containment:
            uf.union(a, b)
            scores[(a, b)] = c

    members: dict[int, list[int]] = defaultdict(list)
    for cid in sh:
        if cid in uf.parent:
            members[uf.find(cid)].append(cid)

    out: dict[int, int] = {}
    group_score: dict[int, float] = {}
    for n, (root, ms) in enumerate(sorted(members.items(), key=lambda kv: -len(kv[1])), 1):
        if len(ms) < 2:
            continue
        for cid in ms:
            out[cid] = n
        rel = [v for (a, b), v in scores.items() if a in ms and b in ms]
        group_score[n] = round(min(rel), 3) if rel else 0.0
    return out, group_score
