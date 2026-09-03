"""임베딩 태스크.
BGE-M3로 벡터 생성 → Qdrant에 Dense + BM25 하이브리드 저장 → tb_document_contents 적재.
상태: 31 → 42 → 11 (실패 시 95).
"""
import threading

from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct,
    Document as QdrantDocument,
    VectorParams,
    Distance,
    SparseVectorParams,
    Modifier,
    PayloadSchemaType,
    HnswConfigDiff,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    QuantizationSearchParams,
    Filter,
    FieldCondition,
    MatchValue,
)

from celery_app import celery_app

from ..config import QDRANT_CONFIG, BM25_CONFIG, StatusCode, task_session
from ..repository import DocumentRepository, ChunkRepository, ContentsRepository
from ..logger import celery_logger as logger

_qdrant = None
_qdrant_lock = threading.Lock()


def _get_qdrant() -> QdrantClient:
    """Qdrant 클라이언트 싱글턴 (double-checked locking). 첫 호출 시 컬렉션 존재 여부도 확인.

    Why 락: `_ensure_collection`이 check-then-create라, `--pool=threads`의 여러 스레드가
    락 없이 진입하면 둘 다 `collection_exists()==False`를 통과해 중복 create_collection으로
    한쪽이 실패한다. embedding.py `get_embedding_model()`·paddle `_get_engine()`과 같은 패턴.
    """
    global _qdrant
    if _qdrant is not None:     # fast path
        return _qdrant
    with _qdrant_lock:
        if _qdrant is None:     # 락 획득 시점 재확인
            # gRPC(protobuf) 사용 시 REST(JSON) 대비 직렬화 크기 2~3배 작음 → 같은 32MB에 더 많이 적재.
            client = QdrantClient(
                host=QDRANT_CONFIG["host"],
                port=QDRANT_CONFIG["port"],
                grpc_port=QDRANT_CONFIG.get("grpc_port", 6334),
                prefer_grpc=True,
            )
            _ensure_collection(client)
            _qdrant = client    # 컬렉션 준비까지 끝난 뒤에 공개 — 반쯤 준비된 객체 노출 방지
    return _qdrant


def _ensure_collection(client: QdrantClient) -> None:
    """컬렉션이 없으면 Dense(1024) + BM25(sparse) 구조로 자동 생성."""
    name = QDRANT_CONFIG["collection_name"]
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": VectorParams(
                    size=QDRANT_CONFIG["vector_size"],
                    distance=Distance.COSINE,
                    # 기본값 명시. recall 부족 시 m=32, ef_construct=200으로 올리고 컬렉션 재생성.
                    hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
                    # 데이터 커졌을 때 재생성 안 하려면 처음부터 켜두기.
                    on_disk=True,
                ),
            },
            sparse_vectors_config={
                BM25_CONFIG["sparse_vector_name"]: SparseVectorParams(
                    modifier=Modifier.IDF,
                ),
            },
            # INT8 양자화: 메모리 4배 절감, recall 손실 1% 미만. 프로덕션 기본.
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    quantile=0.99,
                    always_ram=True,
                ),
            ),
        )
        # 필터에 사용하는 필드는 payload index 필수. 없으면 필터 검색 속도/정확도 급락.
        client.create_payload_index(name, "service_code", PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "document_id", PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "heading_path", PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "page_range[0]", PayloadSchemaType.INTEGER)
        client.create_payload_index(name, "page_range[1]", PayloadSchemaType.INTEGER)
        logger.info(f"[Qdrant] 컬렉션 생성: {name}")
    else:
        logger.info(f"[Qdrant] 컬렉션 존재: {name}")


@celery_app.task(bind=True, name="v1.task.embed.embed_document", max_retries=3)
def embed_document(self, prev_result: dict):
    """청크 벡터화 → Qdrant 저장 → 서빙용 DB 적재."""
    service_code = prev_result["service_code"]
    document_id = prev_result["document_id"]
    document_name = prev_result["document_name"]

    with task_session() as db:
        doc_repo = DocumentRepository(db)
        chunk_repo = ChunkRepository(db)
        contents_repo = ContentsRepository(db)

        try:
            logger.info(f"[임베딩] {document_name}")

            rows = chunk_repo.get_by_document(service_code, document_id)
            if not rows:
                logger.warning(f"[임베딩 스킵] 청크 없음: {document_name}")
                return {"service_code": service_code, "document_id": document_id, "embedded": 0}

            doc_repo.update_status(service_code, document_id, StatusCode.PROCESSING_EMBED)

            from ..utils import embed_texts, count_tokens

            qdrant = _get_qdrant()

            # adaptive: heading_path + content로 문맥 포함 벡터 생성
            # fixed: content만 (heading은 메타데이터에만 유지, LLM 전달 시 옵션으로 합침)
            def _embed_text(r: dict) -> str:
                if r.get("chunk_strategy") == "fixed":
                    return r["content"]
                heading_path = r.get("heading_path") or ""
                content = r["content"]
                if heading_path:
                    return f"{heading_path}\n\n{content}"
                return content

            texts = [_embed_text(r) for r in rows]
            vectors = embed_texts(texts)
            token_counts = count_tokens(texts)

            # BM25 sparse 벡터에 평균 문서 길이를 전달. Qdrant 내부 IDF 계산에 사용.
            avg_len = sum(len(t.split()) for t in texts) / len(texts)

            points = [
                PointStruct(
                    id=r["id"],
                    vector={
                        "dense": vec,
                        BM25_CONFIG["sparse_vector_name"]: QdrantDocument(
                            text=r["content"], model="Qdrant/bm25", options={"avg_len": avg_len}
                        )
                    },
                    payload={
                        "content": r["content"],
                        "service_code": service_code,
                        "document_id": document_id,
                        "seq": r["seq"],
                        "heading": r["heading"],
                        "heading_path": r["heading_path"],
                        "chunk_type": r.get("chunk_type", "text"),
                        "chunk_strategy": r.get("chunk_strategy"),
                        "part_index": r.get("part_index"),
                        "part_total": r.get("part_total"),
                        "page_range": [r["start_page"], r["end_page"]] if r["start_page"] and r["end_page"] else None,
                        "image_paths": r.get("image_paths"),
                        "image_ocr_texts": r.get("image_ocr_texts"),
                    }
                )
                for r, vec in zip(rows, vectors)
            ]

            # 42→43: 임베딩 계산 완료
            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_EMBED)

            # 재임베딩 시 기존 Qdrant 포인트 삭제 후 새로 적재.
            # DB 청크 ID가 바뀌므로 이전 포인트가 남아있으면 검색에 오래된 결과가 걸림.
            qdrant.delete(
                collection_name=QDRANT_CONFIG["collection_name"],
                points_selector=Filter(must=[
                    FieldCondition(key="service_code", match=MatchValue(value=service_code)),
                    FieldCondition(key="document_id", match=MatchValue(value=document_id)),
                ]),
            )

            # 병렬 업로드: 순차 for 루프는 서버가 노는 시간이 생김.
            # parallel=4로 동시 요청 4개 → 인덱싱 쪽에 일감 끊김 없이 공급.
            # batch_size=256: 32MB 제한 안에서 안전(1024차원 INT8 기준 ~1.5MB/배치).
            # OOM 위험은 인덱싱이 못 따라갈 때 발생 → parallel을 더 올리려면 RAM 모니터링 필요.
            qdrant.upload_points(
                collection_name=QDRANT_CONFIG["collection_name"],
                points=points,
                batch_size=256,
                parallel=4,
                wait=True,
            )

            contents_repo.delete_by_document(service_code, document_id)

            # heading_path + content 합친 값을 저장 (Qdrant에 임베딩된 텍스트와 동일)
            contents_rows = [
                {
                    "service_code": service_code,
                    "document_id": document_id,
                    "chunk_id": r["id"],
                    "heading": r["heading"],
                    "heading_path": r["heading_path"],
                    "content": embed_text,
                    "start_page": r["start_page"],
                    "end_page": r["end_page"],
                    "chunk_type": r.get("chunk_type", "text"),
                    "chunk_strategy": r.get("chunk_strategy"),
                    "part_index": r.get("part_index"),
                    "part_total": r.get("part_total"),
                    "image_paths": r.get("image_paths"),
                    "image_ocr_texts": r.get("image_ocr_texts"),
                    "qdrant_point_id": r["id"],   # int — Qdrant point.id와 동일 타입 (chunks.id BIGSERIAL)
                    "token_count": tc,
                    "char_count": len(embed_text),
                }
                for r, tc, embed_text in zip(rows, token_counts, texts)
            ]
            contents_repo.insert_batch(contents_rows)
            db.commit()  # delete + insert 한 트랜잭션으로 commit

            # 43→41: 벡터DB 적재 + DB 적재 완료 (검색 가능 상태)
            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_EMBED_VECTOR)
            doc_repo.update_status(service_code, document_id, StatusCode.COMPLETE_ALL)

            logger.info(f"[임베딩 완료] {document_name} ({len(rows)}개)")

            return {
                "service_code": service_code,
                "document_id": document_id,
                "document_name": document_name,
                "embedded": len(rows),
            }

        except Exception as e:
            logger.error(f"[임베딩 실패] {document_name}: {e}", exc_info=True)

            if self.request.retries < self.max_retries:
                raise self.retry(exc=e, countdown=60)

            try:
                doc_repo.update_status(service_code, document_id, StatusCode.ERROR_EMBED)
            except Exception as cleanup_err:
                logger.warning(f"[상태 업데이트 실패] {document_name}: {cleanup_err}")
            raise
