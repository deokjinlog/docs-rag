"""VL 출력 온전성 게이트 — 격상해서 받아온 결과를 **믿어도 되는지** 판정한다. (캐스케이드 M5)

**설계가 측정에 의해 뒤집힌 지점이다.** 원래 M5 계획은 "OCR 이 못 읽은 페이지를 감지해
VL 로 격상"이었다. 그런데 합성 스캔셋 24장을 페이지별로 분해해 보니 전제가 틀렸다:

    OCR  3-gram  0.591 ~ 0.845   sd 0.068    ← 균일하게 평범. '실패한 페이지'가 없다
    VL   3-gram  0.218 ~ 0.956   sd 0.143    ← 평균은 높은데 분산이 2배 + 파국적 실패

즉 **격상 트리거로 쓸 OCR 실패 부분집합이 존재하지 않는다.** 대신 진짜 위험은 반대편에
있었다 — VL 이 한 장에서 완전히 무너졌고(0.218), 그건 이미지 품질과 무관했다
(같은 페이지의 저해상도·기울기·과노출 변형은 0.80~0.90 으로 멀쩡했다. 오히려 **가장
깨끗한** 입력에서 무너졌다).

무너진 방식은 자기회귀 디코딩의 전형적 실패다 — **루프**. 8-gram 하나("00만원,500")가
556번 반복되며 정답 1,331자짜리 페이지에 4,556자를 뱉었다.

    지표(마크업 제거 후)      정상 23장 최대    실패 1장    마진
    자기반복(1-고유8gram비)      0.252         0.955      3.8배
    최다 8-gram 반복 횟수          8            556        70배

→ 게이트를 "OCR 을 언제 버릴까"가 아니라 **"VL 결과를 받아들여도 되나"** 로 둔다.
   실패하면 OCR 결과로 되돌린다. precision-first 와 같은 규율 — 검증 안 된 출력을
   메인 경로에 넣지 않는다.

**비대칭이 임계를 정한다.** 오탐(멀쩡한 VL 을 버림)은 그 페이지가 OCR 품질(0.65)로
내려앉을 뿐이다. 미탐(루프를 통과시킴)은 "500만원"이 556번 박힌 청크를 색인해
**확신에 찬 오답**의 재료가 된다. 그래서 임계는 정상 최대에서 넉넉히 띄운다.

**마크업을 먼저 벗기는 이유**: VL 은 표를 HTML 로 뱉는데 `style='...word-wrap: break-word;'`
가 셀마다 반복된다. 벗기지 않으면 멀쩡한 표 페이지의 자기반복이 0.646 까지 올라가
진짜 루프(0.955)와 구분이 흐려진다. 벗기면 정상 최대가 0.252 로 내려앉는다.

용법:
    from scripts.vl_sanity import accept_vl
    ok, reason, stats = accept_vl(vl_markdown)
    text = vl_markdown if ok else ocr_text        # 실패 시 싼 티어로 되돌림
"""
import re
from collections import Counter

# ── 임계 ──────────────────────────────────────────────────────────────────────
# 실측(2026-09-08, 합성 스캔셋 24장) 기준 정상 최대의 2~4배. 근사치가 아니라 마진이다.
NGRAM = 8               # 한글 8자 ≈ 짧은 구 하나. 이보다 짧으면 정상 표현도 자주 겹친다
MAX_SELF_REPEAT = 0.50  # 정상 최대 0.252 · 실패 0.955
MAX_NGRAM_COUNT = 30    # 정상 최대 8    · 실패 556
MIN_CHARS = 200         # 이보다 짧으면 통계가 요동친다 — 판정하지 않고 통과

_TAG = re.compile(r"<[^>]{0,400}>")
_WS = re.compile(r"\s+")


def visible_text(md: str) -> str:
    """사람이 읽는 글자만 남긴다 — HTML 태그·속성과 공백 제거.

    표 마크업이 반복 통계를 오염시키는 걸 막는 게 목적이라 태그를 통째로 지운다.
    닫히지 않은 `<` 하나가 뒤를 다 먹는 걸 막으려고 길이 상한(400)을 둔다.
    """
    return _WS.sub("", _TAG.sub(" ", md or ""))


def repetition_stats(md: str, n: int = NGRAM) -> dict:
    """자기반복도와 최다 반복 횟수. 둘은 다른 실패를 잡는다 —
    비율은 '전반적으로 같은 말을 돈다', 횟수는 '한 구절이 폭주한다'."""
    t = visible_text(md)
    if len(t) < n + 1:
        return {"visible_chars": len(t), "self_repeat": 0.0, "max_ngram_count": 0}
    grams = [t[i:i + n] for i in range(len(t) - n + 1)]
    top = Counter(grams).most_common(1)[0][1]
    return {
        "visible_chars": len(t),
        "self_repeat": round(1 - len(set(grams)) / len(grams), 4),
        "max_ngram_count": top,
    }


def accept_vl(md: str) -> tuple[bool, str | None, dict]:
    """VL 출력을 받아들일지 판정. `(ok, reason, stats)`.

    reason 은 거절 사유(통과면 None) — 로그·trace 에 그대로 남겨 왜 되돌렸는지 추적한다.
    """
    st = repetition_stats(md)
    if st["visible_chars"] < MIN_CHARS:
        # 짧은 건 판정 대상이 아니다. 빈 결과 자체는 상위(격상 로직)가 다룬다.
        return True, None, st
    if st["max_ngram_count"] >= MAX_NGRAM_COUNT:
        return False, f"디코딩 루프 의심: {NGRAM}-gram 이 {st['max_ngram_count']}회 반복", st
    if st["self_repeat"] > MAX_SELF_REPEAT:
        return False, f"자기반복 과다: {st['self_repeat']:.3f} > {MAX_SELF_REPEAT}", st
    return True, None, st
