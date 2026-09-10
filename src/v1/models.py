"""SQLAlchemy 모델 — db/schema.sql과 동기 유지가 필수.

8테이블: CodeMaster / DocumentStatus / DocumentStatusLog / DocumentExtract /
DocumentChunk / DocumentContents / QueryFeedback / PageTriage. schema.sql에 컬럼을 추가하면
여기 모델도 동시에 갱신해야 ORM 쿼리가 깨지지 않는다 (FK 미사용 정책 — 운영
단순화 + 부분 삭제·재처리 자유도 우선).
"""

from sqlalchemy import Column, BigInteger, Boolean, Integer, Numeric, String, Text, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from .config.database import Base


class CodeMaster(Base):
    """상태 코드 마스터. status_code → 한글명 룩업 (schema.sql 초기 데이터)."""
    __tablename__ = "tb_code_master"

    code = Column(String(10), primary_key=True)
    code_name = Column(String(100), nullable=False)


class DocumentStatus(Base):
    """현재 상태 스냅샷 (CQRS read model). 원본은 tb_document_status_log."""
    __tablename__ = "tb_document_status"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    document_name = Column(String(255), nullable=False)
    document_path = Column(String(500))
    status_code = Column(String(2), nullable=False, default="00")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime)


class DocumentStatusLog(Base):
    """상태 변경 이력 (append-only). tb_document_status의 원본 로그."""
    __tablename__ = "tb_document_status_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    from_status = Column(String(2))
    to_status = Column(String(2), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class DocumentExtract(Base):
    """PDF→Markdown 추출 원본 보존. 재청킹 시 raw_markdown 재사용."""
    __tablename__ = "tb_document_extract"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    document_name = Column(String(255))
    document_path = Column(String(500))
    total_pages = Column(Integer)
    raw_json = Column(JSONB)
    raw_markdown = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class DocumentChunk(Base):
    """청킹 결과. 재임베딩 시 content 재사용 (벡터DB 적재 전 staging)."""
    __tablename__ = "tb_document_chunks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    seq = Column(Integer, nullable=False)
    heading = Column(Text)
    heading_path = Column(Text)
    content = Column(Text, nullable=False)
    char_count = Column(Integer)
    start_page = Column(Integer)
    end_page = Column(Integer)
    chunk_type = Column(String(16), default="text")
    chunk_strategy = Column(String(16))
    part_index = Column(Integer)
    part_total = Column(Integer)
    image_paths = Column(JSONB)
    image_ocr_texts = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now())


class DocumentContents(Base):
    """검색 서빙 테이블. Qdrant 검색 → qdrant_point_id로 단건 조회 → JOIN 없이 응답."""
    __tablename__ = "tb_document_contents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    chunk_id = Column(BigInteger)
    heading = Column(Text)
    heading_path = Column(Text)
    content = Column(Text)
    start_page = Column(Integer)
    end_page = Column(Integer)
    chunk_type = Column(String(16), default="text")
    chunk_strategy = Column(String(16))
    part_index = Column(Integer)
    part_total = Column(Integer)
    image_paths = Column(JSONB)
    image_ocr_texts = Column(JSONB)
    qdrant_point_id = Column(BigInteger)
    token_count = Column(Integer)
    char_count = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class QueryFeedback(Base):
    """쿼리 피드백 (Insert-only). trace_id로 JSONL trace와 조인.

    FK 없음 — trace가 파일 기반이라 DB FK 불가 + BackgroundTasks race 회피.
    signal은 'up'/'down'/'reformulated' 3종 (DB CHECK + Pydantic Literal 이중 방어).
    집계는 trace_summary.py --feedback.
    """
    __tablename__ = "tb_query_feedback"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    signal = Column(String(20), nullable=False)
    free_text = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())


class PageTriage(Base):
    """페이지 단위 라우팅 판정 + 근거 신호 (extract 직전).

    **왜 신호까지 저장하나**: 판정 자체는 규칙이라 언제든 재계산할 수 있지만, 재계산하려면
    원본 PDF 를 다시 열어야 한다. 그런데 처리가 끝난 문서는 파일이 finished 로 옮겨지고
    나중엔 사라질 수도 있다. "왜 이 페이지가 OCR 로 갔나"를 그때 답하려면 신호가 남아야 한다.
    임계치를 바꿀 때 재처리 없이 분포 변화를 dry-run 하는 것도 이 표가 있어야 된다.

    복합키 (service_code, document_id, page_no) — 기존 전 테이블과 같은 키 형태.
    """
    __tablename__ = "tb_page_triage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_code = Column(String(2), nullable=False)
    document_id = Column(String(255), nullable=False)
    page_no = Column(Integer, nullable=False)
    route = Column(String(20), nullable=False)      # NATIVE | NATIVE_SUSPECT | SCAN
    char_count = Column(Integer)
    garbage_ratio = Column(Numeric(6, 4))
    separator_ratio = Column(Numeric(6, 4))
    image_cover = Column(Numeric(5, 3))
    anchor_hits = Column(Integer)
    font_flags = Column(JSONB)
    conf_stats = Column(JSONB)
    source = Column(String(16), nullable=False)     # pymupdf | odl_tree
    decided_at = Column(DateTime, server_default=func.current_timestamp())
