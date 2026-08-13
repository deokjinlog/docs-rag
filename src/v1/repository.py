"""DB Repository (SQLAlchemy ORM).

각 테이블별 Repository 클래스. flush는 repository, commit은 호출자(task/endpoint)
가 담당 — `delete_by_document` + `insert_chunks` 같은 다단 연산을 한 트랜잭션으로
묶기 위한 규약. tb_document_status는 CQRS read model이라 update_status()만이
log INSERT + status UPDATE 둘 다 처리.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

import logging

from .models import DocumentStatus, DocumentStatusLog, DocumentExtract, DocumentChunk, DocumentContents, CodeMaster, QueryFeedback

logger = logging.getLogger(__name__)


# 문서 상태 (CQRS: log=원본, status=읽기용 스냅샷)
class DocumentRepository:
    """tb_document_status + tb_document_status_log 관리"""

    def __init__(self, db: Session):
        self.db = db

    def create(self, service_code: str, document_id: str, document_name: str,
               document_path: str = None) -> int:
        # 읽기용 스냅샷 INSERT
        doc = DocumentStatus(
            service_code=service_code,
            document_id=document_id,
            document_name=document_name,
            document_path=document_path,
            status_code="00",
        )
        self.db.add(doc)
        # 원본 로그 INSERT (최초 등록)
        self.db.add(DocumentStatusLog(
            service_code=service_code,
            document_id=document_id,
            from_status=None,
            to_status="00",
        ))
        self.db.commit()
        self.db.refresh(doc)
        return doc.id

    def get_by_id(self, service_code: str, document_id: str) -> dict | None:
        row = self.db.query(
            DocumentStatus, CodeMaster.code_name
        ).outerjoin(
            CodeMaster, DocumentStatus.status_code == CodeMaster.code
        ).filter(
            DocumentStatus.service_code == service_code,
            DocumentStatus.document_id == document_id,
        ).first()
        if not row:
            return None
        doc, code_name = row
        return {
            "id": doc.id,
            "service_code": doc.service_code,
            "document_id": doc.document_id,
            "document_name": doc.document_name,
            "document_path": doc.document_path,
            "status_code": doc.status_code,
            "status_name": code_name,
        }

    def update_status(self, service_code: str, document_id: str, status_code: str) -> bool:
        try:
            # 현재 상태 조회 (log의 from_status용)
            current = self.db.query(DocumentStatus).filter(
                DocumentStatus.service_code == service_code,
                DocumentStatus.document_id == document_id,
            ).first()
            from_status = current.status_code if current else None

            # 1. 원본 로그 INSERT (append-only)
            self.db.add(DocumentStatusLog(
                service_code=service_code,
                document_id=document_id,
                from_status=from_status,
                to_status=status_code,
            ))

            # 2. 현재 스냅샷 UPDATE (읽기용)
            if current:
                current.status_code = status_code
                current.updated_at = datetime.now()
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"상태 업데이트 실패: {e}")
            return False


# 추출 원본 보존
class ExtractRepository:
    """tb_document_extract 관리"""

    def __init__(self, db: Session):
        self.db = db

    def upsert(self, service_code: str, document_id: str, document_name: str,
               total_pages: int, raw_json: dict, raw_markdown: str,
               document_path: str = None) -> int:
        existing = self.db.query(DocumentExtract).filter(
            DocumentExtract.service_code == service_code,
            DocumentExtract.document_id == document_id,
        ).first()

        if existing:
            existing.document_name = document_name
            existing.document_path = document_path
            existing.total_pages = total_pages
            existing.raw_json = raw_json
            existing.raw_markdown = raw_markdown
            self.db.commit()
            return existing.id

        ext = DocumentExtract(
            service_code=service_code,
            document_id=document_id,
            document_name=document_name,
            document_path=document_path,
            total_pages=total_pages,
            raw_json=raw_json,
            raw_markdown=raw_markdown,
        )
        self.db.add(ext)
        self.db.commit()
        self.db.refresh(ext)
        return ext.id

    def update_markdown(self, service_code: str, document_id: str, raw_markdown: str) -> None:
        """OCR 처리 후 이미지 태그가 텍스트로 교체된 마크다운을 업데이트."""
        row = self.db.query(DocumentExtract).filter(
            DocumentExtract.service_code == service_code,
            DocumentExtract.document_id == document_id,
        ).first()
        if row:
            row.raw_markdown = raw_markdown
            self.db.commit()

    def get_markdown(self, service_code: str, document_id: str) -> str | None:
        row = self.db.query(DocumentExtract.raw_markdown).filter(
            DocumentExtract.service_code == service_code,
            DocumentExtract.document_id == document_id,
        ).first()
        return row[0] if row else None


# 청크
class ChunkRepository:
    """tb_document_chunks 관리"""

    def __init__(self, db: Session):
        self.db = db

    def insert_chunks(self, chunks: list[dict]) -> tuple[int, list]:
        if not chunks:
            return 0, []
        objs = [
            DocumentChunk(
                service_code=c["service_code"],
                document_id=c["document_id"],
                seq=c["seq"],
                heading=c.get("heading"),
                heading_path=c.get("heading_path"),
                content=c["content"],
                char_count=c.get("char_count"),
                start_page=c.get("start_page"),
                end_page=c.get("end_page"),
                chunk_type=c.get("chunk_type", "text"),
                chunk_strategy=c.get("chunk_strategy"),
                part_index=c.get("part_index"),
                part_total=c.get("part_total"),
                image_paths=c.get("image_paths"),
                image_ocr_texts=c.get("image_ocr_texts"),
            )
            for c in chunks
        ]
        self.db.add_all(objs)
        self.db.flush()  # ID 채번만. commit은 호출자(task)가 담당.
        for obj in objs:
            self.db.refresh(obj)
        return len(objs), objs

    def get_by_document(self, service_code: str, document_id: str) -> list[dict]:
        chunks = self.db.query(DocumentChunk).filter(
            DocumentChunk.service_code == service_code,
            DocumentChunk.document_id == document_id,
        ).order_by(DocumentChunk.seq).all()
        return [
            {
                "id": c.id,
                "seq": c.seq,
                "heading": c.heading,
                "heading_path": c.heading_path,
                "content": c.content,
                "char_count": c.char_count,
                "start_page": c.start_page,
                "end_page": c.end_page,
                "chunk_type": c.chunk_type or "text",
                "chunk_strategy": c.chunk_strategy,
                "part_index": c.part_index,
                "part_total": c.part_total,
                "image_paths": c.image_paths,
                "image_ocr_texts": c.image_ocr_texts,
            }
            for c in chunks
        ]

    def delete_by_document(self, service_code: str, document_id: str) -> int:
        count = self.db.query(DocumentChunk).filter(
            DocumentChunk.service_code == service_code,
            DocumentChunk.document_id == document_id,
        ).delete()
        # commit은 호출자(task)가 담당. delete + insert가 한 트랜잭션으로 묶임.
        return count


# 서빙용 콘텐츠
class ContentsRepository:
    """tb_document_contents 관리"""

    def __init__(self, db: Session):
        self.db = db

    def insert_batch(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        objs = [
            DocumentContents(
                service_code=r["service_code"],
                document_id=r["document_id"],
                chunk_id=r["chunk_id"],
                heading=r.get("heading"),
                heading_path=r.get("heading_path"),
                content=r.get("content"),
                start_page=r.get("start_page"),
                end_page=r.get("end_page"),
                chunk_type=r.get("chunk_type", "text"),
                chunk_strategy=r.get("chunk_strategy"),
                part_index=r.get("part_index"),
                part_total=r.get("part_total"),
                image_paths=r.get("image_paths"),
                image_ocr_texts=r.get("image_ocr_texts"),
                qdrant_point_id=r.get("qdrant_point_id"),
                token_count=r.get("token_count"),
                char_count=r.get("char_count"),
            )
            for r in rows
        ]
        self.db.add_all(objs)
        self.db.flush()  # commit은 호출자(task)가 담당.
        return len(objs)

    def get_by_qdrant_id(self, qdrant_point_id: int) -> dict | None:
        row = self.db.query(DocumentContents).filter(
            DocumentContents.qdrant_point_id == qdrant_point_id,
        ).first()
        if not row:
            return None
        return {
            "id": row.id,
            "service_code": row.service_code,
            "document_id": row.document_id,
            "chunk_id": row.chunk_id,
            "heading": row.heading,
            "heading_path": row.heading_path,
            "content": row.content,
            "start_page": row.start_page,
            "end_page": row.end_page,
            "chunk_type": row.chunk_type or "text",
            "qdrant_point_id": row.qdrant_point_id,
            "char_count": row.char_count,
        }

    def get_by_document(self, service_code: str, document_id: str) -> list[dict]:
        rows = self.db.query(DocumentContents).filter(
            DocumentContents.service_code == service_code,
            DocumentContents.document_id == document_id,
        ).order_by(DocumentContents.id).all()
        return [
            {
                "id": r.id,
                "chunk_id": r.chunk_id,
                "heading": r.heading,
                "content": r.content,
                "start_page": r.start_page,
                "end_page": r.end_page,
                "qdrant_point_id": r.qdrant_point_id,
                "char_count": r.char_count,
            }
            for r in rows
        ]

    def count_by_document(self, service_code: str, document_id: str) -> int:
        return self.db.query(DocumentContents).filter(
            DocumentContents.service_code == service_code,
            DocumentContents.document_id == document_id,
        ).count()

    def delete_by_document(self, service_code: str, document_id: str) -> int:
        count = self.db.query(DocumentContents).filter(
            DocumentContents.service_code == service_code,
            DocumentContents.document_id == document_id,
        ).delete()
        # commit은 호출자(task)가 담당.
        return count


class FeedbackRepository:
    """tb_query_feedback 관리 (Insert-only).

    flush는 repository, commit은 호출자(엔드포인트)가 담당 — '한 요청 = 한 트랜잭션' 규약 유지.
    Update/Delete 메서드 의도적 부재 (Insert-only 설계).
    """

    def __init__(self, db: Session):
        self.db = db

    def insert(self, trace_id: str, signal: str, free_text: str | None = None) -> QueryFeedback:
        fb = QueryFeedback(
            trace_id=trace_id,
            signal=signal,
            free_text=free_text,
        )
        self.db.add(fb)
        self.db.flush()    # id 채번
        self.db.refresh(fb)  # created_at 채번 (server_default)
        return fb


# 관계형 SQL 경로 (schema_insurance.sql — ORM 모델 밖, raw SQL 읽기)
class PayoutRepository:
    """payout_rule 읽기 (SQL 경로 B5). 관계형 스키마는 ORM(models.py) 밖이라 raw SQL SELECT.

    읽기 전용 — 적재는 `scripts/load_payout.py`. rows는 `rag/payout_sql.select_payout`에
    주입돼 결정론 답변에 쓰인다.
    """

    _COLS = (
        "product_id", "coverage", "cause", "age_band", "period_bucket",
        "rate_pct", "per_unit", "limit_days",
        "reduction_rate_pct", "reduction_period", "reduction_cause", "source",
    )

    def __init__(self, db: Session):
        self.db = db

    def get_rules(self, product_id: str | None = None) -> list[dict]:
        """payout_rule 전체(또는 상품 필터) → dict 리스트. rate_pct(Decimal)는 int로 정규화."""
        from sqlalchemy import text
        sql = f"SELECT {', '.join(self._COLS)} FROM payout_rule"
        params: dict = {}
        if product_id:
            sql += " WHERE product_id = :pid"
            params["pid"] = product_id
        out = []
        for row in self.db.execute(text(sql), params).mappings():
            d = dict(row)
            if d.get("rate_pct") is not None:
                d["rate_pct"] = int(d["rate_pct"])   # Decimal → int (골든 정수 비교 일관)
            out.append(d)
        return out

    def get_exclusions(self, product_id: str, coverage_name: str | None = None) -> list[dict]:
        """상품의 **general 면책 조**(지급하지 않는 사유) — 강제첨부용. kind='general'만
        (reduction=감액은 payout_rule에 이미 포함). general 면책은 상품/특약 전체에 적용되므로
        product_id로만 매칭(payout_rule.coverage와 exclusion_map.coverage_name 표기가 달라도
        누락 안 되게 — 예: payout "12개월 소득보장 수술급여금" vs map "약정한 보험금"). clause
        조인으로 조 번호·제목 반환. coverage_name은 API 호환용(현재 미사용).
        """
        from sqlalchemy import text
        sql = (
            "SELECT DISTINCT c.jo, c.title, c.body "
            "FROM coverage_exclusion_map m JOIN clause c ON c.clause_id = m.exclusion_clause "
            "WHERE m.product_id = :pid AND m.kind = 'general' "
            "ORDER BY c.jo"
        )
        rows = self.db.execute(text(sql), {"pid": product_id}).mappings()
        return [dict(r) for r in rows]


class ProductRepository:
    """product 읽기 (관계형 SQL 경로 — 계약조건 terms). raw SQL(ORM 밖)."""

    def __init__(self, db: Session):
        self.db = db

    def get_terms(self, product_id: str | None = None, coverage_kw: str | None = None) -> dict | None:
        """계약조건(청약철회·갱신)용 product row 1건. product_id 우선, 없으면 담보 키워드로
        base 상품(parent_policy_id IS NULL) 해소. 못 찾으면 None(→RAG)."""
        from sqlalchemy import text
        cols = "product_id, product_name, is_renewable, cooling_off_days, resolution_note"
        if product_id:
            sql = f"SELECT {cols} FROM product WHERE product_id = :pid LIMIT 1"
            row = self.db.execute(text(sql), {"pid": product_id}).mappings().first()
        elif coverage_kw:
            sql = (f"SELECT {cols} FROM product "
                   "WHERE parent_policy_id IS NULL AND (product_name LIKE :kw OR coverage_name LIKE :kw) "
                   "LIMIT 1")
            row = self.db.execute(text(sql), {"kw": f"%{coverage_kw}%"}).mappings().first()
        else:
            return None
        return dict(row) if row else None


class CoverageRepository:
    """coverage_range 읽기 (별표3 ICD 보장판정). raw SQL(ORM 밖)."""

    def __init__(self, db: Session):
        self.db = db

    def get_ranges(self, product_id: str | None = None) -> dict:
        """{담보: [코드토큰...]} — coverage_sql.judge_coverage에 주입. product_id 없으면 전체
        (별표3 담보별 코드범위가 있는 상품은 현재 다이렉트뿐)."""
        from sqlalchemy import text
        sql = "SELECT coverage, code_token FROM coverage_range"
        params: dict = {}
        if product_id:
            sql += " WHERE product_id = :pid"
            params["pid"] = product_id
        sql += " ORDER BY id"
        out: dict = {}
        for r in self.db.execute(text(sql), params).mappings():
            out.setdefault(r["coverage"], []).append(r["code_token"])
        return out
