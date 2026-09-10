r"""PDF 추출 태스크.
ODL HTTP API로 PDF를 Markdown + JSON으로 변환.
≤200p: docling-fast 시도 → 실패 시 Java fallback.
>200p: Java-only (docling-fast 스킵).

⚠ 폴백은 **무성 열화**다 — 하이브리드가 죽어도 파이프라인은 통과한다. 그래서 실패는
ERROR 로 남기고, 모드 분포(docling/java/java-direct)를 주기적으로 확인해야 한다:
    docker compose logs celery | grep -oE "\[(docling|java|java-direct)\]$" | sort | uniq -c
docling 이 0 이면 ML 경로가 고장난 것이다(과거 실측 사례: easyocr 미설치).
상태: 00 → 22 → 21 (실패 시 91).
"""

import os
import re
import threading
import json
import shutil
from pathlib import Path

import requests

from celery_app import celery_app
from ..config import INPUT_DIR, OUTPUT_RAW_DIR, FINISHED_DIR, ERROR_DIR, DATA_DIR, StatusCode, task_session
from ..repository import DocumentRepository, ExtractRepository
from ..logger import celery_logger as logger

ODL_URL = os.environ["ODL_URL"]
DOCLING_PAGE_LIMIT = 200
HYBRID_TIMEOUT_MS = 120000
PAGE_SEPARATOR = "<!-- page:%page-number% -->"


# 유틸
def _to_container_path(host_path: Path) -> str:
    """호스트 경로를 ODL 컨테이너 경로로 변환 (./data → /data)."""
    rel = host_path.relative_to(DATA_DIR)
    return f"/data/{rel}"


def _odl_cleanup(host_paths: list[Path]) -> None:
    """ODL 컨테이너가 생성한 파일을 HTTP API로 삭제.
    ODL(UID 1000)과 Worker(UID 1004) 간 UID 불일치로 직접 삭제 불가."""
    targets = [_to_container_path(p) for p in host_paths if p.exists()]
    if targets:
        try:
            requests.post(f"{ODL_URL}/cleanup", json={"paths": targets}, timeout=60)
        except Exception as e:
            logger.warning(f"[cleanup] ODL 삭제 요청 실패: {e}")


def _prune_garbage_images(output_dir: Path, file_stem: str) -> dict:
    """ODL convert 직후 후처리: *_images/ 밑 PNG를 is_valid_image로 걸러서
    garbage(1px/아이콘/단색/비율 과한 것)를 ODL /cleanup API로 삭제.

    ODL이 image_output="external"로 PDF 내부 XObject 전량을 떨구므로(수천~만 장),
    그 중 대부분은 의미 없는 garbage. celery 쪽에 있는 필터 로직을 재사용해서
    생성 직후 바로 정리 → 디스크 절약 + ocr 태스크 iteration 부담 감소.
    """
    from ..utils.ocr import get_image_info, is_valid_image

    images_dir = output_dir / f"{file_stem}_images"
    if not images_dir.is_dir():
        return {"total": 0, "removed": 0, "kept": 0, "breakdown": {}}

    to_delete: list[Path] = []
    breakdown: dict[str, int] = {}
    total = 0
    for png in images_dir.glob("*.png"):
        if png.name.endswith("_ocr_layout.png"):
            continue
        total += 1
        info = get_image_info(png)
        ok, reason = is_valid_image(info)
        if not ok:
            to_delete.append(png)
            breakdown[reason] = breakdown.get(reason, 0) + 1

    if to_delete:
        _odl_cleanup(to_delete)

    return {
        "total": total,
        "removed": len(to_delete),
        "kept": total - len(to_delete),
        "breakdown": breakdown,
    }


def get_page_count(pdf_path: Path) -> int:
    """PDF 바이너리에서 /Count 태그를 스캔하여 페이지 수 추정.
    정확하지 않을 수 있으나, docling-fast 시도 여부 판단용으로 충분."""
    try:
        with open(pdf_path, 'rb') as f:
            content = f.read()
        counts = re.findall(rb'/Count\s+(\d+)', content)
        if counts:
            return max(int(c) for c in counts)
    except Exception:
        pass
    return 200


def check_docling_success(json_path: Path) -> bool:
    """docling-fast 출력 품질 검증.
    heading 타입이 있고 text chunk 타입이 없으면 정상으로 판단."""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return '"type": "heading"' in content and '"type": "text chunk"' not in content
    except Exception:
        return False


# ODL 변환은 **문서 1건씩 직렬**로만 돈다 (D1).
# ODL 서버는 요청마다 Java JVM 을 새로 띄운다. celery 가 `--pool=threads` 라 동시성 N 이면
# JVM 이 N 개 동시에 뜨고, 대형 PDF(1,035p)에서는 그것만으로 WSL 15Gi 를 넘겨 **스택 전체가
# 죽는다**(실측 4회). 동시성을 2 로 낮춰도 JVM 2개가 겹칠 수 있어서 여기서 한 번 더 막는다.
# celery 의 concurrency 와 별개인 이유: concurrency 는 임베딩·청킹까지 함께 제한하는데,
# 그쪽은 병렬로 돌아도 안전하다. 좁게 막아야 처리량을 덜 깎는다.
_ODL_SLOT = threading.Semaphore(1)


# ODL HTTP 변환
def _run_odl(pdf_path: Path, output_dir: Path, hybrid: str = None) -> None:
    """ODL HTTP API에 변환 요청. **JVM 직렬화 슬롯을 통과해야 한다**(_ODL_SLOT).
    hybrid_timeout을 문자열로 넘기는 이유: ODL 내부에서 문자열 파싱."""
    payload = {
        "input_path": _to_container_path(pdf_path),
        "output_dir": _to_container_path(output_dir),
        "format": "json,markdown",
        "image_output": "external",
        "image_format": "png",
        "markdown_page_separator": PAGE_SEPARATOR,
    }
    if hybrid:
        payload["hybrid"] = hybrid
        payload["hybrid_mode"] = "full"
        payload["hybrid_url"] = "http://localhost:5010"
        payload["hybrid_timeout"] = str(HYBRID_TIMEOUT_MS)

    with _ODL_SLOT:                     # JVM 은 한 번에 하나만
        resp = requests.post(f"{ODL_URL}/convert", json=payload, timeout=1800)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)[-500:]
        except Exception:
            detail = resp.text[-500:]
        raise RuntimeError(f"ODL convert 실패: {detail}")


def run_extract(pdf_path: Path, output_dir: Path) -> str:
    """2단계 추출 전략을 실행하고 사용된 모드를 반환."""
    file_stem = pdf_path.stem
    page_count = get_page_count(pdf_path)

    if page_count > DOCLING_PAGE_LIMIT:
        logger.info(f"[java-direct] {pdf_path.name} ({page_count}p > {DOCLING_PAGE_LIMIT}p)")
        _run_odl(pdf_path, output_dir, hybrid=None)
        return "java-direct"

    logger.info(f"[docling-fast] {pdf_path.name} ({page_count}p, timeout={HYBRID_TIMEOUT_MS // 1000}s)")
    try:
        _run_odl(pdf_path, output_dir, hybrid="docling-fast")
    except Exception as e:
        # ERROR로 올린다(WARNING 아님) — 폴백이 있어 파이프라인은 통과하지만 **품질이 조용히
        # 열화**되기 때문. 실측(2026-09-07): 이미지에 easyocr 이 없어 docling 초기화가
        # ImportError 로 죽는 바람에 하이브리드가 **한 번도 성공한 적이 없었고**(모드 분포
        # java 9 · java-direct 1 · docling 0), 58건이 전부 조용히 java 로 떨어졌다.
        # ML 레이아웃 분석이 통째로 빠졌는데 로그 어디에도 "고장"이라는 신호가 없었다.
        # 거절 게이트에서 잡은 '조용한 실패'와 같은 부류 — 폴백은 유지하되 소리는 내게 한다.
        logger.error(f"[docling-fast] 실패 → Java 폴백 (품질 열화): {pdf_path.name} - {e}")

    json_path = output_dir / f"{file_stem}.json"
    if json_path.exists() and check_docling_success(json_path):
        return "docling"

    # docling 품질 미달 → Java fallback. 기존 출력물 삭제 후 재추출.
    logger.warning(f"[fallback] Java 재처리: {pdf_path.name}")
    _odl_cleanup([
        output_dir / f"{file_stem}.json",
        output_dir / f"{file_stem}.md",
        output_dir / f"{file_stem}_images",
    ])

    _run_odl(pdf_path, output_dir, hybrid=None)
    return "java"


# 마크다운 파싱
def parse_markdown_pages(md_path: Path) -> list:
    """ODL이 삽입한 <!-- page:N --> 마커 기준으로 페이지 분리."""
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    pages = []
    parts = re.compile(r'<!-- page:(\d+) -->').split(content)

    if parts[0].strip():
        pages.append({"page": 1, "content": parts[0].strip()})

    for i in range(1, len(parts) - 1, 2):
        page_content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if page_content:
            pages.append({"page": int(parts[i]), "content": page_content})
    return pages


def _record_triage(db, service_code: str, document_id: str, pdf_path: Path) -> None:
    """페이지별 판정·신호를 tb_page_triage 에 적재.

    **실패해도 추출을 막지 않는다** — triage 는 관측이지 추출의 전제가 아니다. 다만
    조용히 넘어가지는 않는다(WARNING). 이 프로젝트에서 무언가가 소리 없이 사라지는 건
    늘 사고의 시작이었다(image 청크 0개가 1년 가까이 안 보였던 것처럼).
    """
    try:
        import pymupdf

        from ..repository import PageTriageRepository
        from ..utils.triage import classify, load_config, signals_from_page

        cfg = load_config()
        rows = []
        with pymupdf.open(pdf_path) as doc:
            for i in range(len(doc)):
                sig = signals_from_page(doc[i], cfg)
                rows.append({
                    "page_no": i + 1, "route": classify(sig, cfg),
                    "char_count": sig.char_count,
                    "garbage_ratio": sig.garbage_ratio,
                    "separator_ratio": sig.separator_ratio,
                    "image_cover": sig.image_cover,
                    "anchor_hits": sig.anchor_hits,
                    "font_flags": sig.font_flags or None,
                    "source": "pymupdf",
                })
        n = PageTriageRepository(db).replace_document(service_code, document_id, rows)
        dist = {}
        for r in rows:
            dist[r["route"]] = dist.get(r["route"], 0) + 1
        logger.info(f"[triage] {pdf_path.name} — {n}p {dist}")
    except Exception as e:
        logger.warning(f"[triage 실패 — 추출은 계속] {pdf_path.name}: {type(e).__name__}: {e}")


# Celery Task
@celery_app.task(bind=True, name="v1.task.extract.extract_pdf", max_retries=3)
def extract_pdf(self, service_code: str, document_id: str, document_name: str):
    """PDF 추출 → 마크다운 파싱 → DB 적재 → finished/ 이동."""
    # Path traversal 방지.
    safe_name = Path(document_name).name
    if safe_name != document_name:
        raise ValueError(f"잘못된 document_name (경로 포함 불가): {document_name}")

    pdf_path = INPUT_DIR / safe_name
    file_stem = Path(safe_name).stem

    with task_session() as db:
        doc_repo = DocumentRepository(db)
        ext_repo = ExtractRepository(db)

        try:
            if not document_name or not document_name.strip():
                raise ValueError(f"document_name이 비어있습니다")
            if not pdf_path.is_file():
                raise FileNotFoundError(f"input 폴더에 파일 없음: {pdf_path}")

            doc_repo.update_status(service_code, document_id, StatusCode.PROCESSING_PDF_EXTRACT)
            logger.info(f"[추출] {document_name}")

            # ── 페이지 판별 (triage) — **추출 직전**에 한다 ──────────────────────
            # 여기가 유일하게 원본 PDF 가 손에 있는 지점이다. 추출이 끝나면 파일은
            # finished/ 로 옮겨지고, 판정을 나중에 하려면 그 파일을 다시 찾아야 한다.
            # 판정 자체는 아직 라우팅을 바꾸지 않는다(경로는 여전히 ODL 단일) — 먼저
            # 관측만 쌓고, 분포를 보고 나서 폴백을 켠다. 이 프로젝트의 순서 그대로다.
            _record_triage(db, service_code, document_id, pdf_path)

            OUTPUT_RAW_DIR.mkdir(parents=True, exist_ok=True)
            extract_mode = run_extract(pdf_path, OUTPUT_RAW_DIR)

            # 후처리: ODL이 덤프한 garbage 이미지(1px/아이콘/단색/비율 과한 것)를
            # is_valid_image로 걸러서 즉시 삭제. 디스크 12000 → 수백 수준으로 감소.
            prune = _prune_garbage_images(OUTPUT_RAW_DIR, file_stem)
            if prune["total"]:
                logger.info(
                    f"[image cleanup] {document_name} — "
                    f"전체 {prune['total']}, 유지 {prune['kept']}, "
                    f"삭제 {prune['removed']} {prune['breakdown']}"
                )

            md_path = OUTPUT_RAW_DIR / f"{file_stem}.md"
            if not md_path.exists():
                raise RuntimeError(f"마크다운 파일 생성 안 됨: {document_name}")

            md_text = md_path.read_text(encoding='utf-8')

            pages = parse_markdown_pages(md_path)
            page_count = len(pages)
            total_chars = sum(len(p["content"]) for p in pages)
            logger.info(f"PDF 로드 완료: {page_count}p, {total_chars:,}자 [{extract_mode}]")

            if not pages:
                raise RuntimeError(f"파싱 결과가 비어있습니다: {document_name}")

            # ODL 원본 JSON을 읽어서 DB에 적재. raw/ 파일은 그대로 보존.
            # PostgreSQL은 JSONB에 \u0000(null byte)을 허용하지 않으므로 제거.
            odl_json_path = OUTPUT_RAW_DIR / f"{file_stem}.json"
            raw_json = None
            if odl_json_path.exists():
                raw_text = odl_json_path.read_text(encoding='utf-8')
                # 실제 null byte + JSON 이스케이프된 \u0000 둘 다 제거
                raw_text = raw_text.replace('\x00', '').replace('\\u0000', '')
                raw_json = json.loads(raw_text)

            ext_repo.upsert(
                service_code=service_code,
                document_id=document_id,
                document_name=document_name,
                total_pages=page_count,
                raw_json=raw_json,
                raw_markdown=md_text,
                document_path=str(pdf_path),
            )

            FINISHED_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf_path), str(FINISHED_DIR / document_name))

            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_PDF_EXTRACT)
            logger.info(f"[추출 완료] {document_name} ({page_count}p, {total_chars:,}자) [{extract_mode}]")

            return {"service_code": service_code, "document_id": document_id, "document_name": document_name}

        except Exception as e:
            logger.error(f"[추출 실패] {document_name}: {e}", exc_info=True)

            if self.request.retries < self.max_retries:
                raise self.retry(exc=e, countdown=60)

            try:
                ERROR_DIR.mkdir(parents=True, exist_ok=True)
                if document_name and pdf_path.is_file():
                    shutil.move(str(pdf_path), str(ERROR_DIR / document_name))
                doc_repo.update_status(service_code, document_id, StatusCode.ERROR_PDF_EXTRACT)
                logger.error(f"[최종 실패] {document_name}: retry 소진, error/로 이동")
            except Exception as cleanup_err:
                logger.warning(f"[cleanup 실패] {document_name}: {cleanup_err}")
            raise
