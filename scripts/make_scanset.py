"""합성 스캔셋 생성 — 네이티브 PDF 를 래스터라이즈해 **정답이 딸린** 스캔 테스트셋을 만든다.
(캐스케이드 M3)

**왜 필요한가** — OCR 과 VL 중 어느 페이지에 뭘 쓸지 정하려면 정확도를 재야 하는데,
스캔 문서에는 정답이 없다. 그런데 우리 코퍼스는 전부 네이티브(triage 실측 94.7%)라
**텍스트 레이어가 곧 정답**이다. 그걸 이미지로 구운 뒤 다시 읽게 하면 CER 을 잴 수 있다.

어제 골든 문서의 원본 PDF 가 없어 회귀를 못 돌린 지점을 이걸로 푼다 — 정답은 우리가
이미 갖고 있었다.

**열화 변형**을 두는 이유: "언제 OCR 로 충분하고 언제 VL 이 필요한가"의 경계를 찾으려면
품질 축이 있어야 한다. 깨끗한 스캔은 OCR 로 충분할 것이고, 어느 지점부터 무너지는지가
격상 임계(M5)의 근거가 된다.

    clean   200dpi                    일반 스캔
    low     120dpi                    저해상도 팩스
    skew    200dpi + 2° 기울기          손으로 올린 스캔
    dark    200dpi + 감마 0.6           복사기 과노출

용법:
    uv run --no-project --with pymupdf --with pillow python scripts/make_scanset.py \\
        --pdf "data/input/KB_22449_약관.pdf" --pages 6 --out data/eval/scanset
"""
import argparse
import json
import pathlib
import sys

# 변형 정의 — dpi 와 후처리. 이름이 곧 결과 디렉토리명이 된다.
VARIANTS = {
    "clean": dict(dpi=200),
    "low":   dict(dpi=120),
    "skew":  dict(dpi=200, angle=2.0),
    "dark":  dict(dpi=200, gamma=0.6),
}

MIN_TRUTH_CHARS = 400   # 정답이 너무 짧으면 CER 이 요동친다
MIN_JO = 2              # '제N조' 가 이만큼은 있어야 본문으로 본다
MAX_DOT_RATIO = 0.05    # 점선 리더('····') 비율 상한 — 목차 페이지 배제

# ⚠ **목차 페이지를 고르면 측정이 망가진다.** 첫 실행에서 글자 수만 보고 골랐더니 p2~p7 이
# 전부 목차였고, 정답의 상당 부분이 점선 리더('························')였다.
# OCR 은 그걸 재현하지 않으니 CER 0.81 이 나왔는데, 그건 OCR 품질이 아니라 **페이지 선택
# 실패**였다. 본문 조문 페이지만 골라야 "글자를 얼마나 정확히 읽나"를 잰다.
import re as _re
_RE_JO = _re.compile(r"제\s*\d+\s*조")
_RE_DOTS = _re.compile(r"[·․‥…．.]{4,}")


def _is_body(text: str) -> bool:
    if len(_RE_JO.findall(text)) < MIN_JO:
        return False
    dots = sum(len(m.group(0)) for m in _RE_DOTS.finditer(text))
    return dots / max(len(text), 1) <= MAX_DOT_RATIO


def _degrade(img, angle: float | None = None, gamma: float | None = None):
    from PIL import Image
    if angle:
        # expand=True 로 잘림 방지. 흰 배경으로 채워 스캔처럼 보이게 한다
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor="white")
    if gamma:
        lut = [min(255, int((i / 255) ** gamma * 255)) for i in range(256)]
        img = img.point(lut * len(img.getbands()))
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", type=int, default=6, help="본문 페이지 몇 장을 쓸지")
    ap.add_argument("--out", default="data/eval/scanset")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    a = ap.parse_args()

    try:
        import pymupdf
        from PIL import Image
    except ImportError:
        print("uv run --no-project --with pymupdf --with pillow python "
              "scripts/make_scanset.py ...", file=sys.stderr)
        return 2

    src = pymupdf.open(a.pdf)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # 본문이 충분한 페이지만 고른다 — 표지·목차는 CER 측정에 부적합
    picked = []
    for i in range(len(src)):
        t = src[i].get_text("text").strip()
        if len(t) >= MIN_TRUTH_CHARS and _is_body(t):
            picked.append((i, t))
        if len(picked) >= a.pages:
            break
    if not picked:
        print(f"본문 페이지 없음(≥{MIN_TRUTH_CHARS}자)", file=sys.stderr)
        return 1

    wanted = [v for v in a.variants.split(",") if v in VARIANTS]
    manifest = []
    for pno, truth in picked:
        (out / "truth").mkdir(exist_ok=True)
        tp = out / "truth" / f"p{pno + 1:04d}.txt"
        tp.write_text(truth, encoding="utf-8")
        for vname in wanted:
            cfg = VARIANTS[vname]
            zoom = cfg["dpi"] / 72
            pix = src[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img = _degrade(img, cfg.get("angle"), cfg.get("gamma"))
            vd = out / vname
            vd.mkdir(exist_ok=True)
            ip = vd / f"p{pno + 1:04d}.png"
            img.save(ip)
            manifest.append({"page": pno + 1, "variant": vname,
                             "image": str(ip.relative_to(out)),
                             "truth": str(tp.relative_to(out)),
                             "truth_chars": len(truth),
                             "dpi": cfg["dpi"], "size": [img.width, img.height]})
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    src.close()

    print(f"  스캔셋: {out}")
    print(f"  페이지 {len(picked)} × 변형 {len(wanted)} = {len(manifest)}장")
    for pno, t in picked:
        print(f"    p{pno + 1}  정답 {len(t):5d}자")
    print(f"  manifest: {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
