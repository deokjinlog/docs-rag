"""보험 조/별표 벡터 색인 (step 5) — clause/annex → insurance_bge_m3_1024.

관계형(정확한 데이터, SQL 경로)과 분리된 RAG 해석 계층. 조 본문을 Dense(BGE-M3) +
BM25 sparse 하이브리드로 임베딩하되 **product_id를 payload로 박아 필터링** → DRM(라이나
특약들의 near-identical 제8·9조가 서로 오염되는 것)을 원천 차단. 별표는 fetch 경로라
summary만 안전망으로 색인(실제 값은 SQL annex/annex_row).

point id는 clause_id/annex_id에서 uuid5로 결정론 생성 → 재색인 멱등(덮어쓰기).

용법(컨테이너 내):
  docker compose exec api uv run python scripts/index_insurance.py [product_id ...]
  (인자 없으면 product 테이블 전체)
"""
import os
import sys
import uuid
import json
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, Document as QdrantDocument, VectorParams, Distance,
    SparseVectorParams, Modifier, PayloadSchemaType, HnswConfigDiff,
    ScalarQuantization, ScalarQuantizationConfig, ScalarType,
    Filter, FieldCondition, MatchValue,
)

from src.v1.config import QDRANT_CONFIG, INSURANCE_COLLECTION, BM25_CONFIG, task_session

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")   # 고정 네임스페이스 (결정론 id)
_SPARSE = BM25_CONFIG["sparse_vector_name"]
# 임베딩은 이미 떠 있는 api 서버(/embeddings)에 위임한다. 8GB에서 스크립트가 BGE-M3를
# 별도로 또 로드하면 서버 사본과 겹쳐 OOM(SIGKILL) — 서버의 로드된 모델을 재사용.
_EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8002/api/v1/docs-rag/embeddings")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """api /embeddings 위임(작은 배치). 스크립트 내 2차 모델 로드 회피 + 8GB 박스라
    한 번에 많이 인코딩하면 서버 BGE-M3가 OOM → 배치 8로 피크 메모리 억제."""
    out = []
    for i in range(0, len(texts), 8):
        req = urllib.request.Request(
            _EMBED_URL, data=json.dumps({"texts": texts[i:i + 8]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.load(r)["vectors"])
    return out


def _pid(ref_id: str) -> str:
    return str(uuid.uuid5(_NS, ref_id))


def ensure_collection(qc: QdrantClient) -> None:
    """조/별표 색인용 컬렉션: Dense(1024)+BM25, product_id/kind payload 인덱스.
    product_id는 필터 tenant 키라 인덱스 필수(없으면 필터 검색 정확도·속도 급락)."""
    if qc.collection_exists(INSURANCE_COLLECTION):
        return
    qc.create_collection(
        collection_name=INSURANCE_COLLECTION,
        vectors_config={"dense": VectorParams(
            size=QDRANT_CONFIG["vector_size"], distance=Distance.COSINE,
            hnsw_config=HnswConfigDiff(m=16, ef_construct=100), on_disk=True)},
        sparse_vectors_config={_SPARSE: SparseVectorParams(modifier=Modifier.IDF)},
        quantization_config=ScalarQuantization(scalar=ScalarQuantizationConfig(
            type=ScalarType.INT8, quantile=0.99, always_ram=True)),
    )
    for field in ("product_id", "kind", "company"):
        qc.create_payload_index(INSURANCE_COLLECTION, field, PayloadSchemaType.KEYWORD)
    qc.create_payload_index(INSURANCE_COLLECTION, "jo", PayloadSchemaType.INTEGER)
    print(f"[Qdrant] 컬렉션 생성: {INSURANCE_COLLECTION}")


def _load(product_ids: list[str]) -> list[dict]:
    """clause(조 본문) + annex(별표 요약) 레코드를 색인 단위로 로드."""
    where = "WHERE p.product_id = ANY(:ids)" if product_ids else ""  # 리스트 → PG 배열
    params = {"ids": list(product_ids)} if product_ids else {}
    items = []
    with task_session() as db:
        clauses = db.execute(text(
            f"""SELECT c.clause_id, c.product_id, c.jo, c.title, c.body,
                       p.company, p.product_name
                FROM clause c JOIN product p ON p.product_id=c.product_id {where}
                ORDER BY c.product_id, c.jo"""), params).mappings().all()
        for c in clauses:
            # 임베딩 텍스트: 상품명(제품 간 구분) + 조 본문. payload 필터와 이중 안전.
            # BGE-M3 sweet spot(~512토큰) + 8GB 메모리 감안해 1600자로 컷(초과분은
            # 검색 근거로 덜 중요한 조 후반부 — 본문 전체는 clause.body/SQL에 보존).
            embed = f"{c['product_name']}\n{c['body']}"[:1600]
            items.append({
                "point_id": _pid(c["clause_id"]), "kind": "clause",
                "embed": embed, "text": c["body"],
                "product_id": c["product_id"], "company": c["company"],
                "product_name": c["product_name"], "jo": c["jo"],
                "ref_id": c["clause_id"], "title": c["title"],
            })
        annexes = db.execute(text(
            f"""SELECT a.annex_id, a.product_id, a.annex_no, a.title, a.kind AS akind,
                       a.summary, p.company, p.product_name
                FROM annex a JOIN product p ON p.product_id=a.product_id {where}
                ORDER BY a.product_id, a.annex_no"""), params).mappings().all()
        for a in annexes:
            embed = f"{a['product_name']} 별표{a['annex_no']} {a['title']}\n{a['summary']}"
            items.append({
                "point_id": _pid(a["annex_id"]), "kind": "annex",
                "embed": embed, "text": a["summary"],
                "product_id": a["product_id"], "company": a["company"],
                "product_name": a["product_name"], "jo": None,
                "ref_id": a["annex_id"], "title": f"별표{a['annex_no']} {a['title']}",
                "annex_kind": a["akind"],
            })
    return items


def index(product_ids: list[str]) -> int:
    items = _load(product_ids)
    if not items:
        print("색인 대상 없음"); return 0
    qc = QdrantClient(host=QDRANT_CONFIG["host"], port=QDRANT_CONFIG["port"],
                      grpc_port=QDRANT_CONFIG["grpc_port"], prefer_grpc=True)
    ensure_collection(qc)

    vectors = embed_texts([it["embed"] for it in items])
    avg_len = sum(len(it["embed"].split()) for it in items) / len(items)
    points = [PointStruct(
        id=it["point_id"],
        vector={"dense": vec, _SPARSE: QdrantDocument(
            text=it["embed"], model="Qdrant/bm25", options={"avg_len": avg_len})},
        payload={k: it[k] for k in ("kind", "text", "product_id", "company",
                                     "product_name", "jo", "ref_id", "title")
                 } | ({"annex_kind": it["annex_kind"]} if "annex_kind" in it else {}),
    ) for it, vec in zip(items, vectors)]

    # 멱등: 이 상품들의 기존 포인트 삭제 후 재적재 (uuid5라 사실상 덮어쓰기지만 명시)
    pids = sorted({it["product_id"] for it in items})
    for pid in pids:
        qc.delete(INSURANCE_COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="product_id", match=MatchValue(value=pid))]))
    qc.upload_points(INSURANCE_COLLECTION, points=points, batch_size=256, parallel=2, wait=True)

    n_cl = sum(1 for it in items if it["kind"] == "clause")
    n_an = len(items) - n_cl
    print(f"[색인 완료] {INSURANCE_COLLECTION} · 상품 {pids} · 조 {n_cl} + 별표 {n_an} = {len(points)}점")
    return len(points)


def _search(qc: QdrantClient, query: str, product_id: str | None, top_k: int = 5):
    """product_id 필터 하이브리드(Dense+BM25 RRF). 필터가 DRM(제품 간 오염) 차단."""
    from qdrant_client.models import Prefetch, FusionQuery, Fusion
    qvec = embed_texts([query])[0]
    flt = (Filter(must=[FieldCondition(key="product_id", match=MatchValue(value=product_id))])
           if product_id else None)
    res = qc.query_points(
        INSURANCE_COLLECTION,
        prefetch=[
            Prefetch(query=qvec, using="dense", limit=top_k * 4, filter=flt),
            Prefetch(query=QdrantDocument(text=query, model="Qdrant/bm25"),
                     using=_SPARSE, limit=top_k * 4, filter=flt),
        ],
        query=FusionQuery(fusion=Fusion.RRF), limit=top_k, with_payload=True,
    )
    return res.points


def demo():
    qc = QdrantClient(host=QDRANT_CONFIG["host"], port=QDRANT_CONFIG["port"],
                      grpc_port=QDRANT_CONFIG["grpc_port"], prefer_grpc=True)
    queries = [
        ("중환자실에 입원하면 보험금을 얼마나 받나요?", "LINA_ICU_2024"),
        ("보험금을 지급하지 않는 경우가 있나요?", "LINA_ICU_2024"),
        ("보험금은 어떻게 청구하나요?", "LINA_ICU_2024"),
    ]
    for q, pid in queries:
        print(f"\nQ: {q}   [filter: product_id={pid}]")
        for p in _search(qc, q, pid)[:3]:
            pl = p.payload
            print(f"   {p.score:.3f} [{pl['kind']:<6}] {pl['ref_id'].split('_', 2)[-1]:<8} · {pl['title'][:34]}")


def demo_drm():
    """DRM: 필터 없으면 라이나 특약들의 near-identical 제8조가 섞인다(어느 상품 것?).
    product_id 필터가 이를 격리 — 관계형 payload가 벡터검색을 결정론적으로 만든다."""
    qc = QdrantClient(host=QDRANT_CONFIG["host"], port=QDRANT_CONFIG["port"],
                      grpc_port=QDRANT_CONFIG["grpc_port"], prefer_grpc=True)
    q = "보험금은 어떻게 청구하나요?"

    def _show(pid, tag):
        print(f"\n── {tag} ──")
        for p in _search(qc, q, pid)[:4]:
            pl = p.payload
            print(f"   {p.score:.3f} [{pl['product_id']:<16}] {pl['ref_id'].split('_', 2)[-1]:<7} {pl['title'][:22]}")

    print(f"Q: {q}")
    _show(None, "필터 없음 → 제품 간 오염(DRM): 어느 상품 제8조인지 섞임")
    _show("LINA_ICU_2024", "product_id=LINA_ICU_2024 필터 → 중환자실 특약만")
    _show("LINA_INCOME_2024", "product_id=LINA_INCOME_2024 필터 → 소득보장 특약만")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--demo":
        demo()
    elif mode == "--drm":
        demo_drm()
    else:
        index(sys.argv[1:])
