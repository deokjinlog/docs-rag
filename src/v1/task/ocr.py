"""OCR 태스크.

ODL이 떨군 이미지 파일들을 개별적으로 필터해 의미있는 것만 paddle에 보낸다:
  [1] is_valid_image 6단계 입구 필터 → 장식/구분선/아이콘/단색 차단
  [2] paddle 처리 후 블록 분류:
       - drop(header/footer/page_number) → 청크 생성 제외
       - table 라벨 → chunk_type="table" 개별 청크
       - 나머지 콘텐츠 블록 → 합쳐서 chunk_type="image" 한 청크
같은 이미지에서 나온 image/table 청크는 같은 heading_path를 공유해
sibling 복원 단계에서 한 섹션으로 묶여 LLM에 전달된다.

상태: 21 → 24 → 23 (실패 시 92).
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from celery_app import celery_app

from ..config import OUTPUT_RAW_DIR, StatusCode, task_session

CHUNKER_TYPE = os.environ["CHUNKER_TYPE"]
from ..repository import DocumentRepository, ExtractRepository, ChunkRepository
from ..logger import celery_logger as logger

# 정규식은 preprocess 에 단일 정의 — 여기·청커·preprocess 세 곳에 복제돼 있다가 서로
# 달라져서 이 태스크가 통째로 no-op 이 됐던 전례가 있다(아래 상세). 다시 복제하지 말 것.
from ..utils.preprocess import IMAGE_TAG_RE as _IMAGE_TAG_RE
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_PAGE_RE = re.compile(r'<!-- page:(\d+) -->')


def _text_similarity(a: str, b: str) -> float:
    """2-gram Jaccard — heading 중복 탐지용."""
    if not a or not b:
        return 0.0
    ngrams_a = set(a[i:i+2] for i in range(len(a)-1))
    ngrams_b = set(b[i:i+2] for i in range(len(b)-1))
    if not ngrams_a or not ngrams_b:
        return 0.0
    return len(ngrams_a & ngrams_b) / len(ngrams_a | ngrams_b)


def _count_pages(md_text: str) -> int:
    pages = _PAGE_RE.findall(md_text)
    return max(len(pages), 1) if pages else 1


def _find_context_for_image(md_text: str, image_pos: int) -> dict:
    """이미지 태그 위치에서 가장 가까운 heading과 page를 찾는다."""
    before_text = md_text[:image_pos]

    heading = None
    heading_path = None
    headings = list(_HEADING_RE.finditer(before_text))
    if headings:
        last_heading = headings[-1]
        heading = last_heading.group(2).strip()
        heading_path = heading

    page = None
    pages = list(_PAGE_RE.finditer(before_text))
    if pages:
        page = int(pages[-1].group(1))

    return {"heading": heading, "heading_path": heading_path, "page": page}


@celery_app.task(bind=True, name="v1.task.ocr.ocr_images", max_retries=1)
def ocr_images(self, prev_result: dict):
    service_code = prev_result["service_code"]
    document_id = prev_result["document_id"]
    document_name = prev_result["document_name"]

    with task_session() as db:
        doc_repo = DocumentRepository(db)
        ext_repo = ExtractRepository(db)
        chunk_repo = ChunkRepository(db)

        try:
            doc_repo.update_status(service_code, document_id, StatusCode.PROCESSING_OCR)

            md_text = ext_repo.get_markdown(service_code, document_id)
            if not md_text:
                logger.warning(f"[OCR 스킵] 마크다운 없음: {document_name}")
                return prev_result

            image_matches = list(_IMAGE_TAG_RE.finditer(md_text))
            if not image_matches:
                # 이 로그가 **정상**인지 **정규식이 못 잡은 것**인지 구분되어야 한다.
                # 실제로 후자였다: ODL 은 `![](<경로.png>)` 를 뱉는데 정규식은
                # `![image N](경로.png)` 를 기대해 매칭 0건 → 매 문서가 여기로 빠졌고
                # paddle 은 호출된 적이 없다(코퍼스 image 청크 0개). 이미지 파일이
                # 디스크에 있는데 태그를 못 잡았으면 WARNING 으로 올린다.
                imgs = OUTPUT_RAW_DIR.glob(f"{document_name.rsplit('.', 1)[0]}_images/*.png")
                orphan = sum(1 for _ in imgs)
                if orphan:
                    logger.warning(
                        f"[OCR 스킵] 태그 미검출인데 이미지 파일 {orphan}개 존재: "
                        f"{document_name} — 마크다운 이미지 표기와 IMAGE_TAG_RE 불일치 의심"
                    )
                else:
                    logger.info(f"[OCR 스킵] 이미지 없음: {document_name}")
                doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_OCR)
                return prev_result

            page_count = _count_pages(md_text)
            logger.info(
                f"[OCR 진입] {document_name} "
                f"(페이지 {page_count}, {len(image_matches)}개 이미지)"
            )

            from ..utils.ocr import get_image_info, is_valid_image, ocr_image, is_meaningful_ocr_result

            heading_texts = {
                h_match.group(2).strip().lower()
                for h_match in _HEADING_RE.finditer(md_text)
            }

            ocr_chunks: list[dict] = []
            total = len(image_matches)
            skipped_empty = 0
            skipped_noise = 0
            skipped_dup = 0
            skipped_drop = 0  # drop 블록만 있는 이미지
            kept_text = 0     # chunk_type=image 생성
            kept_table = 0    # chunk_type=table 생성

            filter_stats = {
                "file_size": 0, "too_small": 0, "icon_size": 0,
                "aspect_h": 0, "aspect_v": 0, "too_large": 0,
                "flat_color": 0, "not_exists": 0,
            }

            # [1단계] is_valid_image 입구 필터 통과한 이미지만 수집
            valid_images = []
            for match in image_matches:
                image_rel_path = match.group(1)
                image_path = OUTPUT_RAW_DIR / image_rel_path
                info = get_image_info(image_path)
                ok, reason = is_valid_image(info)
                if not ok:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                    continue
                valid_images.append((match, image_rel_path, image_path))
            skipped_size = sum(filter_stats.values())

            # paddle HTTP 호출 — 이미지별 I/O 바운드라 ThreadPool 병렬
            ocr_results: dict = {}
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(ocr_image, img_path): (match, rel_path, img_path)
                    for match, rel_path, img_path in valid_images
                }
                for future in as_completed(futures):
                    match, rel_path, img_path = futures[future]
                    try:
                        ocr_results[(match, rel_path, img_path)] = future.result()
                    except Exception as e:
                        # ERROR로 올린다(WARNING 아님) — 빈 결과로 대체하면 파이프라인은
                        # 통과하지만 **image/table 청크가 조용히 사라진다**. docling 하이브리드가
                        # 같은 패턴으로 한 번도 안 돌고 있었던 전례가 있다(1fd1cfb).
                        # 실측(2026-09-08): PP-StructureV3 가 paddle 3.3.1 + paddleocr 3.4.0
                        # 조합에서 oneDNN/PIR 실행기 버그로 추론 시 죽는다 —
                        #   NotImplementedError: ConvertPirAttribute2RuntimeAttribute
                        #   not support [pir::ArrayAttribute<pir::DoubleAttribute>]
                        #   (onednn_instruction.cc:116)
                        # FLAGS_use_mkldnn/FLAGS_enable_pir_api 를 env 로 꺼도 안 먹는다
                        # (paddlex 가 자체 Config 로 predictor 를 만들어 paddle FLAGS 가 안 닿음).
                        # 그 결과 코퍼스의 image 청크가 **0개**다(text 7,882 · table 851 · image 0).
                        logger.error(f"[OCR 실패 → 빈 결과로 대체(품질 열화)] {img_path}: {e}")
                        ocr_results[(match, rel_path, img_path)] = {"text": "", "tables": [], "dropped": []}

            # [2단계] 결과 분류 → image 청크 + table 청크 생성
            for match, image_rel_path, image_path in valid_images:
                ocr_data = ocr_results.get(
                    (match, image_rel_path, image_path),
                    {"text": "", "tables": [], "dropped": []},
                )
                if isinstance(ocr_data, str):  # 이전 버전 호환
                    ocr_data = {"text": ocr_data, "tables": [], "dropped": []}

                ocr_text = (ocr_data.get("text") or "").strip()
                ocr_tables = ocr_data.get("tables") or []
                ocr_dropped = ocr_data.get("dropped") or []

                if not ocr_text and not ocr_tables:
                    if ocr_dropped:
                        skipped_drop += 1
                    else:
                        skipped_empty += 1
                    continue

                context = _find_context_for_image(md_text, match.start())
                base_chunk = {
                    "service_code": service_code,
                    "document_id": document_id,
                    "seq": 0,
                    "heading": context["heading"],
                    "heading_path": context["heading_path"],
                    "start_page": context["page"],
                    "end_page": context["page"],
                    "chunk_strategy": CHUNKER_TYPE,
                    "image_paths": [image_rel_path],
                }

                # image 청크 — 콘텐츠 블록(title/text/caption/formula 등) 합친 것
                if ocr_text:
                    if not is_meaningful_ocr_result(ocr_text):
                        skipped_noise += 1
                    else:
                        ocr_lower = ocr_text.lower().replace(" ", "")
                        is_dup = any(
                            _text_similarity(ocr_lower, h.replace(" ", "")) > 0.7
                            for h in heading_texts
                        )
                        if is_dup:
                            skipped_dup += 1
                        else:
                            kept_text += 1
                            ocr_chunks.append({
                                **base_chunk,
                                "content": ocr_text,
                                "char_count": len(ocr_text),
                                "chunk_type": "image",
                                "image_ocr_texts": [ocr_text],
                            })

                # table 청크 — paddle이 분리한 표 각각 (heading_path 공유, sibling에서 묶임)
                for table_md in ocr_tables:
                    t = table_md.strip()
                    if len(t) < 10:
                        continue
                    kept_table += 1
                    ocr_chunks.append({
                        **base_chunk,
                        "content": t,
                        "char_count": len(t),
                        "chunk_type": "table",
                        "image_ocr_texts": [t],
                    })

            filter_breakdown = ", ".join(f"{k}:{v}" for k, v in filter_stats.items() if v > 0)
            logger.info(
                f"[OCR 통계] {document_name} — "
                f"전체 {total}, 입구필터 {skipped_size} [{filter_breakdown}], "
                f"빈값 {skipped_empty}, drop전용 {skipped_drop}, "
                f"노이즈 {skipped_noise}, 중복 {skipped_dup}, "
                f"저장(image) {kept_text}, 저장(table) {kept_table}"
            )

            if ocr_chunks:
                inserted, _ = chunk_repo.insert_chunks(ocr_chunks)
                logger.info(f"[OCR 완료] {document_name} ({inserted}개 청크 생성)")

            cleaned_md = _IMAGE_TAG_RE.sub('', md_text)
            ext_repo.update_markdown(service_code, document_id, cleaned_md)

            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_OCR)
            return prev_result

        except Exception as e:
            logger.error(f"[OCR 실패] {document_name}: {e}", exc_info=True)
            try:
                doc_repo.update_status(service_code, document_id, StatusCode.ERROR_OCR)
            except Exception:
                pass
            raise
