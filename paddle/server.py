"""PaddleOCR PP-StructureV3 HTTP API.

이미지 파일을 받아 레이아웃 분석 + 표 + 수식 + OCR을 수행하고,
콘텐츠 블록을 chunk_type 기준(drop/table/text)으로 분류해 반환한다.
디바이스는 env `PADDLE_DEVICE` (기본 cpu, GPU 는 `docker-compose.paddle-gpu.yml` 오버레이).
**CPU 는 동작하는 기본값이 아니다** — oneDNN/PIR 버그로 추론 시 프로세스가 죽는다.
(과거 주석의 'Blackwell sm_120 미지원' 은 다른 머신 이야기였고, 이 머신 RTX 4060 에선
 원인이 CUDA compat 라이브러리가 WSL 드라이버를 가린 것이었다.)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("paddle-server")

app = FastAPI(title="Paddle OCR API", version="0.2.0")

# -----------------------------------------------------------------------------
# 엔진 초기화 — "준비는 1번만, 사용은 병렬로" 패턴
#
# PPStructureV3() 생성자는 서브모델 5종(layout/det/rec/table/formula)을 다 로드해서
# 수십 초 + 수 GB. 공유 자원(엔진)은 싱글톤으로 두고, 실제 OCR(predict)은 락 없이
# 여러 스레드가 동시에 두드린다. 엔진은 stateless 추론기라 공유 안전.
#
# 원래 버그: lazy init이 락 없으면, celery ThreadPoolExecutor(4)의 첫 배치 4개가
# "if _engine is None" 체크를 동시에 통과 → 4개 스레드가 각자 생성 → 서브모델
# 5종 × 4세트 중복 로드로 메모리 피크 OOM 각.
#
# 수정: double-checked locking — fast path는 락 없이 바로 return, 첫 호출만 락
# 잡고 안에서 한 번 더 체크(다른 스레드가 먼저 만들었을 수 있음). 추가로 startup
# warmup으로 첫 요청이 로딩 지연을 떠안지 않게 컨테이너 시작 시점에 생성 앞당김.
# -----------------------------------------------------------------------------
_engine = None
_engine_lock = threading.Lock()

# 저장 시점 점수 컷 — paddle 결과에 confidence가 낮은 garbage가 섞여 들어오면
# _ocr.json / _ocr_layout.png가 수천 개 쌓이므로, 의미 있는 검출만 저장한다.
# 의미 기반 필터(drop 라벨/한글비율/중복)는 여전히 청킹 단계 책임.
LAYOUT_MIN_SCORE = 0.5   # layout detector 박스 confidence 컷
REC_MIN_SCORE = 0.5      # 개별 텍스트 라인 OCR confidence 컷


# 실행 디바이스 — 기본은 CPU(오버레이 없이 뜨는 구성). GPU 는 오버레이가 켠다:
#   docker compose -f docker-compose.yml -f docker-compose.paddle-gpu.yml up -d paddle
#
# ⚠ **CPU 는 안전한 기본값이 아니다.** 실측(2026-09-08~09): paddle 3.3.1 + paddleocr 3.4.0
# 조합에서 CPU 추론이 oneDNN/PIR 실행기 버그로 **프로세스째 죽는다**
#   NotImplementedError: ConvertPirAttribute2RuntimeAttribute
#   not support [pir::ArrayAttribute<pir::DoubleAttribute>]  (onednn_instruction.cc:116)
# 요청 하나가 컨테이너를 재시작시키고, ocr.py 의 ThreadPool(4)가 이를 반복한다.
# 그 결과 코퍼스 image 청크가 **0개**였다(text 7,882 · table 851 · image 0).
# GPU 경로는 같은 이미지에서 정상 동작한다 — "OCR 이 고장났다"가 아니라 "CPU 로 돌려서
# 고장났다"가 정확한 진단이다. 그래서 부팅 시 어느 쪽인지 로그에 명시적으로 남긴다.
DEVICE = os.environ.get("PADDLE_DEVICE", "cpu").strip() or "cpu"


def _get_engine():
    """PPStructureV3 엔진 싱글톤 (double-checked locking).

    mkldnn 은 **CPU 경로에서만** 의미가 있고, 그 경로가 위 버그의 발원지다. GPU 일 땐
    끄고 말고 할 대상이 아니라서 건드리지 않는다(끄는 호출 자체가 GPU predictor 구성에
    불필요한 간섭).
    """
    global _engine
    if _engine is not None:     # fast path — 이미 준비됨, 락 없이 바로 return
        return _engine
    with _engine_lock:          # 첫 호출만 락 잡음. 나머지 동시 요청은 여기서 대기
        if _engine is None:     # 락 획득 시점엔 다른 스레드가 먼저 만들었을 수 있음 → 재확인
            import paddle
            on_cpu = DEVICE.startswith("cpu")
            if on_cpu:
                try:
                    paddle.set_flags({"FLAGS_use_mkldnn": False})
                except Exception as e:
                    logger.warning(f"[engine] paddle.set_flags 실패: {e}")
            else:
                # GPU 를 요청했는데 실제로 못 잡으면 **조용히 CPU 로 흘러가면 안 된다** —
                # 그건 컨테이너가 죽는 경로다. 여기서 시끄럽게 실패시켜 원인을 드러낸다.
                # (실측 원인 1위: LD_LIBRARY_PATH 의 cuda compat 이 WSL 드라이버를 가림.
                #  오버레이가 /usr/lib/wsl/lib 를 앞에 붙이는 이유 — troubleshooting-boot.md)
                if paddle.device.cuda.device_count() < 1:
                    raise RuntimeError(
                        f"PADDLE_DEVICE={DEVICE} 인데 CUDA 디바이스가 0개다. "
                        "LD_LIBRARY_PATH 에 /usr/lib/wsl/lib 가 앞에 오는지 확인할 것"
                    )
                paddle.set_device(DEVICE)
            from paddleocr import PPStructureV3
            kwargs = dict(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                lang="korean",
                device=DEVICE,
            )
            if on_cpu:
                kwargs["enable_mkldnn"] = False
            _engine = PPStructureV3(**kwargs)
            logger.info(
                f"PPStructureV3 초기화 완료 ({DEVICE}, layout+table+formula+OCR)"
                + ("  ⚠ CPU 추론은 oneDNN/PIR 버그로 죽는다 — paddle-gpu 오버레이 권장"
                   if on_cpu else "")
            )
        return _engine


@app.on_event("startup")
def _warmup_engine():
    """첫 요청이 서브모델 5종 로딩을 떠안지 않도록 startup에서 미리 로드.
    락은 warmup이 실패하거나 타이밍이 어긋나는 경우를 위한 안전망으로 남김."""
    _get_engine()


# -----------------------------------------------------------------------------
# 블록 라벨 분류 — 출처(ODL vs paddle)가 아니라 "내용 구조"가 기준
# -----------------------------------------------------------------------------
# drop: boilerplate (페이지 번호/헤더/푸터/러닝 타이틀 등) — 청크 생성 제외
_DROP_LABELS = {
    "header", "footer", "page_header", "page_footer",
    "page_number", "page_no", "running_title",
    "seal", "stamp", "watermark",
}
# table: 표 구조 → chunk_type="table"로 별도 저장
_TABLE_LABELS = {"table", "tableau"}
# 그 외 (text/title/paragraph_title/doc_title/caption/formula/reference/image 등)
# → 같은 이미지 내 콘텐츠 블록을 합쳐 chunk_type="image"로 저장


def _extract_blocks(res: Any) -> list[dict]:
    """PP-StructureV3 result → 분류된 block list.

    반환 block:
      {"type": "text"  | "table" | "drop", "text": str, "html": str?, "_label": str}
    """
    blocks: list[dict] = []

    # 1순위: parsing_res_list (레이아웃 분석 결과, 가장 정확)
    try:
        prl = res["parsing_res_list"]
        if prl:
            for item in prl:
                if isinstance(item, dict):
                    label = item.get("label") or item.get("type") or "unknown"
                    text = item.get("text") or item.get("content") or ""
                    html = item.get("html")
                else:
                    label = getattr(item, "label", None) or getattr(item, "region_label", "unknown")
                    text = getattr(item, "content", "") or getattr(item, "text", "")
                    html = getattr(item, "html", None)

                label_norm = str(label).lower().strip()
                text_str = text.strip() if text else ""

                if label_norm in _DROP_LABELS:
                    blocks.append({"type": "drop", "_label": label_norm, "text": text_str})
                    continue

                if label_norm in _TABLE_LABELS:
                    if html or text_str:
                        blocks.append({
                            "type": "table",
                            "text": text_str,
                            "html": html or "",
                            "_label": label_norm,
                        })
                    continue

                if text_str:
                    blocks.append({
                        "type": "text",
                        "text": text_str,
                        "_label": label_norm,
                    })
            return blocks
    except (KeyError, TypeError):
        pass

    # 폴백 1: overall_ocr_res의 raw 라인 (parsing_res_list가 빈 경우)
    # 필터 없이 전부 합친다. 품질 컷은 celery 청킹 단계에서.
    try:
        ocr_res = res["overall_ocr_res"]
        rec_texts = ocr_res["rec_texts"]
        if rec_texts:
            lines = [str(t) for t in rec_texts if str(t).strip()]
            if lines:
                blocks.append({"type": "text", "text": "\n".join(lines), "_label": "fallback_ocr"})
    except (KeyError, TypeError):
        pass

    # 폴백 2: table_res_list (parsing에서 표를 못 잡은 경우)
    try:
        table_list = res["table_res_list"]
        if table_list:
            for table in table_list:
                html = table.get("html", "") if isinstance(table, dict) else ""
                text = table.get("text", "") if isinstance(table, dict) else ""
                if html or text:
                    blocks.append({"type": "table", "text": text, "html": html, "_label": "table"})
    except (KeyError, TypeError):
        pass

    return blocks


# -----------------------------------------------------------------------------
# 레이아웃 시각화 (_ocr_layout.png)
# -----------------------------------------------------------------------------
_LABEL_COLORS = {
    "table": (0, 0, 255),       # 빨강
    "figure": (255, 0, 0),      # 파랑
    "text": (0, 200, 0),        # 초록
    "title": (0, 165, 255),     # 주황
    "header": (255, 255, 0),    # 시안
    "footer": (128, 128, 128),  # 회색
}
_DEFAULT_COLOR = (0, 200, 200)


def _extract_bbox(box: Any) -> list[int] | None:
    """PaddleOCR 버전별 bbox 필드 차이 흡수."""
    if isinstance(box, dict):
        for key in ("coordinate", "bbox", "box", "rect"):
            if key in box:
                return [int(c) for c in box[key]]
    for attr in ("coordinate", "bbox", "box", "rect"):
        val = getattr(box, attr, None)
        if val is not None:
            return [int(c) for c in val]
    return None


def _save_layout_image(
    image_path: Path,
    layout_boxes: list[Any],
    output_path: Path,
    text_boxes: list[dict] | None = None,
) -> None:
    """레이아웃 블록 시각화. text_boxes가 있으면 개별 텍스트 라인도 자홍색으로 덧그림
    (환경변수 PADDLE_VIZ_TEXT_LINES=1 일 때만 전달됨, 디버깅용)."""
    img = cv2.imread(str(image_path))
    if img is None:
        return

    h, w = img.shape[:2]
    short_side = min(h, w)
    thickness = max(2, min(6, short_side // 300))
    font_scale = max(0.5, min(1.5, short_side / 1000))
    label_thickness = max(1, thickness - 1)

    for box in layout_boxes:
        label = box.get("label", "") if isinstance(box, dict) else getattr(box, "label", "")
        score = box.get("score", 0) if isinstance(box, dict) else getattr(box, "score", 0)
        bbox = _extract_bbox(box)
        if not bbox or len(bbox) < 4:
            continue

        color = _LABEL_COLORS.get(label.lower(), _DEFAULT_COLOR)
        x1, y1, x2, y2 = bbox[:4]
        cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)

        text = f"{label} {float(score):.2f}"
        (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, label_thickness)
        label_y = max(y1 - 6, th + 6)
        cv2.rectangle(img, (x1, label_y - th - 6), (x1 + tw + 6, label_y + baseline), color, -1)
        cv2.putText(
            img, text, (x1 + 3, label_y - 3),
            cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255),
            label_thickness, cv2.LINE_AA,
        )

    if text_boxes:
        TEXT_LINE_COLOR = (255, 0, 255)  # 자홍
        line_thickness = max(1, thickness - 1)
        line_font_scale = max(0.35, font_scale * 0.6)
        for tb in text_boxes:
            bbox = tb.get("bbox")
            score = float(tb.get("score", 0))
            if not bbox or len(bbox) < 4:
                continue
            x1, y1, x2, y2 = bbox[:4]
            cv2.rectangle(img, (x1, y1), (x2, y2), TEXT_LINE_COLOR, line_thickness)
            label_txt = f"{score:.2f}"
            (tw, th), baseline = cv2.getTextSize(label_txt, cv2.FONT_HERSHEY_SIMPLEX, line_font_scale, 1)
            label_y = min(y2 + th + 4, h - 2)
            cv2.rectangle(img, (x1, label_y - th - 4), (x1 + tw + 4, label_y + baseline), TEXT_LINE_COLOR, -1)
            cv2.putText(
                img, label_txt, (x1 + 2, label_y - 2),
                cv2.FONT_HERSHEY_SIMPLEX, line_font_scale, (255, 255, 255),
                1, cv2.LINE_AA,
            )

    cv2.imwrite(str(output_path), img)
    logger.info(f"[layout] 시각화 저장: {output_path}")


# -----------------------------------------------------------------------------
# /ocr 엔드포인트
# -----------------------------------------------------------------------------
# NOTE: 이전에 Adaptive Threshold + Median Blur 전처리가 있었지만 제거함.
# PP-StructureV3 layout detector는 자연 RGB 문서 이미지로 학습돼서 이진화 입력을
# 받으면 layout_boxes가 1/10로 줄고 라벨이 "image" 한 덩어리로 뭉침. 측정으로 확인.
class OCRRequest(BaseModel):
    image_path: str
    doc_id: str | None = None
    page: int | None = None


def _save_ocr_json(path: Path, payload: dict) -> None:
    try:
        ocr_json_path = path.with_name(f"{path.stem}_ocr.json")
        with open(ocr_json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[ocr] _ocr.json 저장 실패 {path}: {e}")


def _conf_stats(raw: list[float], kept: list[float]) -> dict:
    """줄 confidence 의 **컷 전** 분포.

    ⚠ 이게 없으면 신호를 못 쓴다. 응답에는 REC_MIN_SCORE 를 통과한 점수만 담기는데,
    낮은 줄을 버린 뒤의 평균은 항상 높다 — "이 페이지 OCR 이 얼마나 위태로운가"를
    가리키는 정보가 컷과 함께 사라진다.
    실측(합성 스캔셋 24장): `conf<0.8 줄 비율` 이 3-gram 정확도와 r=-0.808 로 상관하고,
    열화 수준을 고정한 변형 내부에서도 -0.48~-0.91 로 유지된다. 즉 이건 '저해상도라
    당연히 낮다'가 아니라 **페이지 고유의 신호**다. VL 격상 판단의 1차 입력이 된다.
    """
    n = len(raw)
    if not n:
        return {"n": 0, "mean": None, "lt80_ratio": None, "cut": 0, "kept": len(kept)}
    return {
        "n": n,
        "mean": round(sum(raw) / n, 4),
        "median": round(sorted(raw)[n // 2], 4),
        "lt80_ratio": round(sum(1 for x in raw if x < 0.8) / n, 4),
        "cut": n - len(kept),          # REC_MIN_SCORE 에 걸려 버려진 줄 수
        "kept": len(kept),
    }


@app.post("/ocr")
def ocr(req: OCRRequest):
    """이미지 한 장 OCR → _ocr.json / _ocr_layout.png 저장 + 분류된 blocks 반환."""
    path = Path(req.image_path)
    if not path.is_file():
        raise HTTPException(404, f"파일 없음: {req.image_path}")

    logger.info(f"[ocr] {req.image_path}")

    try:
        engine = _get_engine()
        try:
            results = list(engine.predict(str(path)))
        except IndexError as e:
            # PaddleX NMS 버그: layout detector 박스 0~1개면 boxes[:, :6]가 1D로 터짐
            if "1-dimensional" in str(e):
                logger.warning(f"[ocr] layout det 박스 부족: {req.image_path} ({e})")
                return {"status": "ok", "block_count": 0, "blocks": [], "skipped": True}
            raise

        if not results:
            logger.warning(f"[ocr] predict 결과 없음: {req.image_path}")
            return {"status": "ok", "block_count": 0, "blocks": [], "skipped": True}

        viz_text_lines = os.environ.get("PADDLE_VIZ_TEXT_LINES", "0") == "1"
        all_blocks: list[dict] = []
        # 응답은 이미지 1장 단위인데 predict 는 여러 result 를 낼 수 있다 → 누적해서 한 번에
        all_raw_scores: list[float] = []
        all_kept_scores: list[float] = []

        for res in results:
            raw_boxes_all = res["layout_det_res"]["boxes"] if res["layout_det_res"]["boxes"] else []

            # 저장용 박스 — LAYOUT_MIN_SCORE 이상만. raw에는 저점수 garbage가 섞여 있어서
            # 컷하지 않으면 _ocr.json/_ocr_layout.png가 불필요하게 비대해짐.
            kept_raw_boxes = []
            layout_boxes_with_coords = []
            for b in raw_boxes_all:
                label = b["label"] if isinstance(b, dict) else getattr(b, "label", "")
                score = float(b["score"] if isinstance(b, dict) else getattr(b, "score", 0))
                if score < LAYOUT_MIN_SCORE:
                    continue
                bbox = _extract_bbox(b)
                entry = {"label": label, "score": score}
                if bbox:
                    entry["bbox"] = bbox
                layout_boxes_with_coords.append(entry)
                kept_raw_boxes.append(b)

            # 개별 텍스트 라인 — 원본 전부 (JSON 저장 + 디버그 시각화용)
            ocr_sub = res["overall_ocr_res"] if "overall_ocr_res" in res else {}
            raw_rec_texts = list(ocr_sub.get("rec_texts", []))
            raw_rec_scores = [float(s) for s in ocr_sub.get("rec_scores", [])]
            raw_rec_polys = ocr_sub.get("rec_polys") or ocr_sub.get("dt_polys") or []
            try:
                raw_rec_polys = list(raw_rec_polys)
            except Exception:
                raw_rec_polys = []

            # rec_texts 저장용 — REC_MIN_SCORE 이상만
            kept_rec_texts: list[str] = []
            kept_rec_scores: list[float] = []
            all_raw_scores.extend(raw_rec_scores)
            kept_text_boxes: list[dict] = []
            for i, (rt, rs) in enumerate(zip(raw_rec_texts, raw_rec_scores)):
                if rs < REC_MIN_SCORE or not str(rt).strip():
                    continue
                kept_rec_texts.append(rt)
                kept_rec_scores.append(rs)
                all_kept_scores.append(rs)
                if i < len(raw_rec_polys):
                    poly = raw_rec_polys[i]
                    try:
                        pts = poly.tolist() if hasattr(poly, "tolist") else list(poly)
                        if pts:
                            if len(pts) == 4 and not isinstance(pts[0], (list, tuple)):
                                bbox_xyxy = [int(pts[0]), int(pts[1]), int(pts[2]), int(pts[3])]
                            else:
                                xs = [int(p[0]) for p in pts]
                                ys = [int(p[1]) for p in pts]
                                bbox_xyxy = [min(xs), min(ys), max(xs), max(ys)]
                            kept_text_boxes.append({"bbox": bbox_xyxy, "score": rs, "text": rt})
                    except Exception:
                        pass

            # 점수 컷 후 남은 게 하나도 없으면 — 의미 있는 검출이 없는 garbage 이미지.
            # 시각화(_ocr_layout.png)는 생략하지만 **_ocr.json 은 남긴다**.
            # 파일이 없으면 "빈 결과"와 "아직 처리 안 함"이 구분되지 않는다 — 실제로
            # 451장 중 94장이 그래서 커버리지 79.2% 로 보였고, 재실행할 때마다 같은 94장을
            # 다시 처리하게 된다(끝나지 않는 배치). empty 플래그로 그 둘을 가른다.
            if not layout_boxes_with_coords and not kept_rec_texts:
                logger.info(f"[ocr] 빈 결과(점수 컷) — empty 로 기록: {req.image_path}")
                _save_ocr_json(path, {
                    "empty": True,
                    "input_path": str(res["input_path"]) if res["input_path"] else None,
                    "width": res["width"], "height": res["height"],
                    "rec_texts": [], "rec_scores": [], "layout_boxes": [],
                    "parsing_blocks": [],
                    "raw_line_count": len(raw_rec_texts),   # 컷 전엔 몇 줄이었나 — 진단용
                    "raw_box_count": len(raw_boxes_all),
                    "conf_stats": _conf_stats(raw_rec_scores, kept_rec_scores),
                })
                continue

            # _ocr.json 저장
            _save_ocr_json(path, {
                "input_path": str(res["input_path"]) if res["input_path"] else None,
                "width": res["width"],
                "height": res["height"],
                "rec_texts": kept_rec_texts,
                "rec_scores": kept_rec_scores,
                "conf_stats": _conf_stats(raw_rec_scores, kept_rec_scores),
                "layout_boxes": layout_boxes_with_coords,
                "parsing_blocks": [
                    {"label": getattr(p, "label", ""), "content": getattr(p, "content", "")}
                    for p in res["parsing_res_list"]
                ] if res["parsing_res_list"] else [],
            })

            # 레이아웃 시각화 — 점수 컷 통과한 박스만 그림
            try:
                layout_img_path = path.with_name(f"{path.stem}_ocr_layout.png")
                _save_layout_image(
                    path, kept_raw_boxes, layout_img_path,
                    text_boxes=kept_text_boxes if viz_text_lines else None,
                )
            except Exception as e:
                logger.warning(f"[ocr] _ocr_layout.png 저장 실패 {req.image_path}: {e}")

            all_blocks.extend(_extract_blocks(res))

        return {"status": "ok", "block_count": len(all_blocks), "blocks": all_blocks,
                "conf_stats": _conf_stats(all_raw_scores, all_kept_scores)}

    except HTTPException:
        raise
    except Exception as e:
        error_id = str(uuid4())[:8]
        logger.error(f"[ocr] 실패 [{error_id}]: {e}", exc_info=True)
        raise HTTPException(500, f"OCR 실패. error_id: {error_id}")
    finally:
        # ⚠ **8GB 카드에서는 이게 없으면 배치가 못 끝난다.** paddle 의 할당자는 캐시를
        # 드라이버에 돌려주지 않아서, 요청을 이어 던지면 풀이 카드를 통째로 먹고
        # (실측: 7,921 / 8,188 MiB) 그 다음 할당이 cudaErrorMemoryAllocation 으로 죽는다.
        # 실측(2026-09-09): 전량 배치 403장 중 95장까지 성공 → 이후 308장 전부 500.
        # 이미지가 커서가 아니다(p50 0.93MP · 10MP 초과 6장뿐) — 순전히 미반환 누적이다.
        _release_gpu()


def _release_gpu() -> None:
    """요청 사이에 GPU 캐시를 드라이버에 반환. CPU 모드면 아무것도 안 한다."""
    if DEVICE.startswith("cpu"):
        return
    try:
        import paddle
        paddle.device.cuda.empty_cache()
    except Exception as e:          # 반환 실패가 요청을 죽이면 안 된다
        logger.warning(f"[gpu] empty_cache 실패: {e}")


@app.get("/health")
def health():
    return {"status": "ok"}
