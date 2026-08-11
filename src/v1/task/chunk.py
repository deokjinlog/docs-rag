"""청킹 태스크.
마크다운을 CHUNKER_TYPE 전략에 따라 청크로 분할 → DB 적재.
재청킹 시 tb_document_extract.raw_markdown에서 원본을 다시 로드.
상태: 21 → 32 → 31 (실패 시 93).
"""

import os
import re
import json
from collections import defaultdict
from pathlib import Path

from celery_app import celery_app

from ..config import OUTPUT_RAW_DIR, OUTPUT_PROCESSED_DIR, StatusCode, task_session
from ..repository import DocumentRepository, ExtractRepository, ChunkRepository, ContentsRepository
from ..utils.chunker import chunk_markdown, to_json

CHUNKER_TYPE = os.environ["CHUNKER_TYPE"]
from ..logger import celery_logger as logger


def _merge_ocr_chunks(text_chunks: list[dict], ocr_chunks: list[dict]) -> list[dict]:
    """OCR 청크를 heading_path/page 기준으로 텍스트 청크 사이에 삽입.
    같은 heading_path가 있으면 해당 그룹의 마지막에, 없으면 가장 가까운 page 뒤에 삽입."""
    if not ocr_chunks:
        return text_chunks

    result = list(text_chunks)

    for ocr in ocr_chunks:
        ocr_hp = ocr.get("heading_path")
        ocr_page = ocr.get("start_page") or 0
        insert_pos = len(result)

        if ocr_hp:
            last_match = -1
            for i, chunk in enumerate(result):
                if chunk.get("heading_path") == ocr_hp:
                    last_match = i
            if last_match >= 0:
                insert_pos = last_match + 1

        else:
            for i, chunk in enumerate(result):
                chunk_page = chunk.get("start_page") or 0
                if chunk_page > ocr_page:
                    insert_pos = i
                    break

        result.insert(insert_pos, ocr)

    return result


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _strip_media_noise(chunks: list[dict]) -> list[dict]:
    """모든 청크(text + OCR image/table) content의 <br>를 공백으로 — 임베딩 노이즈 제거.
    텍스트 청크는 청커(_repair_markdown_headings)에서 이미 처리되지만 OCR 표 청크는 그
    경로를 안 거치고(celery ocr.py 생성) 셀 줄바꿈이 <br>로 남으므로, 전 청크가 합류한
    이 수렴점에서 통일한다. 표 구조 파이프(|)는 보존, <br>만 제거."""
    for c in chunks:
        cont = c.get("content")
        if cont and "<br" in cont.lower():
            cont = _BR_RE.sub(" ", cont)
            cont = re.sub(r" {2,}", " ", cont)
            c["content"] = cont
    return chunks


def _reassign_part_indices(chunks: list[dict]) -> list[dict]:
    """heading_path 그룹별 part_index/part_total을 재부여.
    같은 heading_path를 가진 청크가 여러 개면 sibling 그룹으로 묶임."""
    groups = defaultdict(list)
    for i, chunk in enumerate(chunks):
        hp = chunk.get("heading_path")
        if hp:
            groups[hp].append(i)

    for hp, indices in groups.items():
        total = len(indices)
        for part_idx, chunk_idx in enumerate(indices, 1):
            chunks[chunk_idx]["part_index"] = part_idx
            chunks[chunk_idx]["part_total"] = total

    # heading_path 없는 청크는 sibling 그룹에 속하지 않으므로 단독 처리.
    for chunk in chunks:
        if not chunk.get("heading_path"):
            chunk["part_index"] = 1
            chunk["part_total"] = 1

    return chunks


def _load_markdown(ext_repo: ExtractRepository, service_code: str, document_id: str, file_stem: str) -> str:
    """DB 우선, 파일 폴백. 재청킹 시에도 원본 마크다운 보장."""
    md_text = ext_repo.get_markdown(service_code, document_id)
    if md_text:
        return md_text
    return (OUTPUT_RAW_DIR / f"{file_stem}.md").read_text(encoding="utf-8")


def _preserve_existing_ocr(
    chunk_repo: ChunkRepository,
    contents_repo: ContentsRepository,
    service_code: str,
    document_id: str,
) -> list[dict]:
    """기존 청크/콘텐츠 삭제 전, OCR이 만든 image+table 청크를 보존.

    이걸 미리 떼놓지 않으면 재청킹 시 OCR 결과가 영구 손실됨.
    image만 챙기면 같은 이미지에서 분리된 table 청크가 사라지므로 둘 다 보존.
    """
    contents_repo.delete_by_document(service_code, document_id)
    existing_ocr = [
        c for c in chunk_repo.get_by_document(service_code, document_id)
        if c.get("chunk_type") in ("image", "table")
    ]
    for ocr in existing_ocr:
        ocr["service_code"] = service_code
        ocr["document_id"] = document_id
    chunk_repo.delete_by_document(service_code, document_id)
    return existing_ocr


def _build_text_chunks(chunk_dicts: list[dict], service_code: str, document_id: str) -> list[dict]:
    """chunker 출력(text/table)을 DB row 형태로 변환."""
    text_chunks = []
    for cd in chunk_dicts:
        heading_path = cd.get("heading_path", [])
        text_chunks.append({
            "service_code": service_code,
            "document_id": document_id,
            "heading": cd.get("heading"),
            "heading_path": " > ".join(heading_path) if heading_path else None,
            "content": cd["content"],
            "char_count": cd.get("char_count"),
            "start_page": cd.get("page_start"),
            "end_page": cd.get("page_end"),
            "chunk_type": cd.get("chunk_type", "text"),
            "chunk_strategy": CHUNKER_TYPE,
            "part_index": cd.get("part_index"),
            "part_total": cd.get("part_total"),
        })
    return text_chunks


def _save_chunks_json(file_stem: str, chunks: list[dict]) -> None:
    """OCR 합류·part_index 재부여 이후의 최종 형태를 디스크에 보존 (디버그·재현)."""
    OUTPUT_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PROCESSED_DIR / f"{file_stem}_chunks.json", "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)


@celery_app.task(bind=True, name="v1.task.chunk.chunk_document", max_retries=3)
def chunk_document(self, prev_result: dict):
    """마크다운 청킹 → JSON 저장 → DB 적재."""
    service_code = prev_result["service_code"]
    document_id = prev_result["document_id"]
    document_name = Path(prev_result["document_name"]).name
    file_stem = Path(document_name).stem

    with task_session() as db:
        doc_repo = DocumentRepository(db)
        ext_repo = ExtractRepository(db)
        chunk_repo = ChunkRepository(db)
        contents_repo = ContentsRepository(db)

        try:
            doc_repo.update_status(service_code, document_id, StatusCode.PROCESSING_PREPROCESS)
            logger.info(f"[청킹] {document_name}")

            md_text = _load_markdown(ext_repo, service_code, document_id, file_stem)
            chunk_objs = chunk_markdown(md_text, source_file=document_name, service_code=service_code)
            chunk_dicts = to_json(chunk_objs)

            existing_ocr = _preserve_existing_ocr(chunk_repo, contents_repo, service_code, document_id)
            text_chunks = _build_text_chunks(chunk_dicts, service_code, document_id)

            all_chunks = _merge_ocr_chunks(text_chunks, existing_ocr)
            all_chunks = _strip_media_noise(all_chunks)   # <br> 등 미디어 노이즈 통일 제거(OCR 표 포함)
            all_chunks = _reassign_part_indices(all_chunks)
            for i, chunk in enumerate(all_chunks, 1):
                chunk["seq"] = i

            _save_chunks_json(file_stem, all_chunks)

            inserted, _ = chunk_repo.insert_chunks(all_chunks)
            db.commit()  # delete + insert 한 트랜잭션으로 commit

            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_PREPROCESS)
            logger.info(f"[청킹 완료] {document_name} ({inserted}개)")

            return {
                "service_code": service_code,
                "document_id": document_id,
                "document_name": document_name,
                "chunks": inserted,
            }

        except Exception as e:
            logger.error(f"[청킹 실패] {document_name}: {e}", exc_info=True)

            if self.request.retries < self.max_retries:
                raise self.retry(exc=e, countdown=60)

            try:
                doc_repo.update_status(service_code, document_id, StatusCode.ERROR_PREPROCESS)
            except Exception as cleanup_err:
                logger.warning(f"[상태 업데이트 실패] {document_name}: {cleanup_err}")
            raise
