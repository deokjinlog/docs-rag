# REST API 설계

Base URL: `/api/v1/docs-rag`

---

## 엔드포인트 요약

| Method | Path | 설명 | 멱등성 |
|--------|------|------|--------|
| POST | `/documents` | 문서 등록 + 파이프라인 발행 | X (매번 새 ID) |
| GET | `/documents/{service_code}/{document_id}` | 문서 상태 조회 | O |
| POST | `/retrieve` | 벡터 검색 | O |
| POST | `/answer` | RAG 질의응답 | 준멱등 (*) |
| POST | `/embeddings` | 텍스트 → 벡터 변환 | O |
| POST | `/feedback` | 쿼리 피드백 수집 (trace_id 기반) | X (매번 새 row) |

(*) CRAG 재시도 횟수는 달라질 수 있으나 최종 답변은 동일.

---

## 1. POST /documents

문서를 등록하고 비동기 파이프라인(extract→ocr→chunk→embed)을 발행한다.

### Request

```json
{
  "service_code": "01",
  "document_id": "0001",
  "document_name": "운전자상해보험_약관.pdf",
  "document_path": "/path/to/file"
}
```

| 필드 | 타입 | 필수 | 제한 | 설명 |
|------|------|------|------|------|
| service_code | string | O | max 10 | 서비스 구분 (01=AI_PARSER) |
| document_id | string | O | max 255 | 문서 식별자 |
| document_name | string | O | max 500 | PDF 파일명 |
| document_path | string | X | max 500 | 원본 경로 (메타데이터용) |

### Response (200)

```json
{
  "id": 1,
  "message": "등록 완료"
}
```

### 에러

| 코드 | 원인 |
|------|------|
| 422 | 필수 필드 누락, 길이 초과 |
| 500 | DB 등록 실패 |

### 멱등성 고려

- **비멱등**: 같은 document_id로 재호출 시 UNIQUE 제약 위반 에러.
- **재처리**: 기존 문서를 재처리하려면 DB에서 삭제 후 재등록하거나, 별도 재처리 API 필요.
- **부분 실패**: 문서 등록은 성공했지만 Celery 태스크 발행이 실패할 수 있음. status_code로 확인.

---

## 2. GET /documents/{service_code}/{document_id}

문서의 현재 파이프라인 상태를 조회한다.

### Response (200)

```json
{
  "id": 1,
  "service_code": "01",
  "document_id": "0001",
  "document_name": "약관.pdf",
  "document_path": "/path",
  "status_code": "11",
  "status_name": "완료(전체)"
}
```

### 상태 코드 값

| status_code | 의미 |
|-------------|------|
| 00 | 대기 |
| 22 → 21 | PDF 추출 중 → 완료 |
| 24 → 23 | OCR 중 → 완료 |
| 32 → 31 | 청킹 중 → 완료 |
| 42 → 43 → 41 | 임베딩 중 → 완료 → 벡터DB 적재 |
| 11 | 전체 완료 |
| 91~99 | 에러 (단계별) |

### 에러

| 코드 | 원인 |
|------|------|
| 404 | 문서를 찾을 수 없음 |

---

## 3. POST /retrieve

하이브리드 검색(Dense + BM25 + RRF) + CrossEncoder 리랭킹 + Sibling 복원.

### Request

```json
{
  "query": "보험금 청구 절차가 어떻게 되나요?",
  "service_code": "01",
  "document_id": null,
  "start_page": null,
  "end_page": null,
  "include_keywords": null,
  "exclude_keywords": null,
  "top_k": 10
}
```

| 필드 | 타입 | 필수 | 제한 | 설명 |
|------|------|------|------|------|
| query | string | O | max 2000 | 검색 쿼리 |
| service_code | string | X | max 10 | 서비스 필터 |
| document_id | string | X | max 255 | 문서 필터 |
| start_page | int | X | 1~99999 | 시작 페이지 필터 |
| end_page | int | X | 1~99999 | 끝 페이지 필터 |
| include_keywords | list[str] | X | max 20개 | 포함 키워드 (AND) |
| exclude_keywords | list[str] | X | max 20개 | 제외 키워드 |
| top_k | int | X | 1~100, 기본 10 | 반환 결과 수 |

### Response (200)

```json
{
  "trace_id": "abc-123-def-456",
  "query": "보험금 청구 절차가 어떻게 되나요?",
  "total": 3,
  "elapsed_ms": 380,
  "sources": [
    {
      "chunk_id": "121",
      "page_range": [15, 15],
      "content": "보험수익자는 다음의 서류를 제출하고...",
      "chunk_type": "text",
      "rrf_score": 0.0312,
      "rerank_score": 0.8721
    },
    {
      "chunk_id": "122",
      "page_range": [15, 16],
      "content": "| 구분 | 지급률 |...",
      "chunk_type": "image",
      "rrf_score": 0.0280,
      "rerank_score": 0.7510,
      "image_paths": ["약관_images/img8.png"]
    }
  ],
  "context": "## 제7조 보험금의 청구\n\n보험수익자는...\n\n---\n\n## 제8조...",
  "route": {
    "strategy": "dense_heavy",
    "query_type": "procedure"
  }
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| sources[].chunk_id | 항상 | Qdrant point ID — `/answer` 응답의 `citations[].supported_by_chunks` 매핑 키 |
| sources[].image_paths | image 청크만 | OCR 원본 이미지 경로 |
| context | 항상 | Sibling 복원 후 heading 포함 마크다운 |
| route.strategy | 항상 | bm25_heavy / dense_heavy / hybrid |
| route.query_type | 항상 | structured_lookup / interpretation / procedure / comparison / simple_fact |

### 에러

| 코드 | 원인 |
|------|------|
| 422 | query 누락, top_k 범위 초과, 길이 초과 |
| 500 | Qdrant 연결 실패, 컬렉션 미존재 |

---

## 4. POST /answer

검색 + CRAG 루프 + 프롬프트 분기 + LLM 답변 생성 + Self-RAG 검증.

### Request

`/retrieve`와 동일한 필드. `top_k` 기본값만 다름 (3, 범위 1~20).

```json
{
  "query": "무면허운전 시 보험금 지급이 되나요?",
  "service_code": "01",
  "top_k": 3
}
```

### Response (200)

```json
{
  "trace_id": "abc-123-def-456",
  "query": "무면허운전 시 보험금 지급이 되나요?",
  "answer": "- **쟁점**: 무면허운전 시 보험금 지급 여부\n- **규정**: ...\n- **결론**: 지급되지 않습니다.",
  "elapsed_ms": 2340,
  "sources": [
    {"chunk_id": "121", "page_range": [42, 42], "content": "...", "rerank_score": 0.87},
    {"chunk_id": "122", "page_range": [43, 43], "content": "...", "rerank_score": 0.72}
  ],
  "citations": [
    {
      "claim": "무면허운전 시 보험금이 지급되지 않습니다",
      "refs": ["제43조"],
      "supported_by_chunks": ["121", "122"]
    }
  ],
  "route": {
    "strategy": "dense_heavy",
    "query_type": "interpretation"
  },
  "verification": {
    "risk_level": "hard_fail",
    "groundedness": 0.50,
    "warnings": ["context에 없는 조항 참조: 제99조"]
  }
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| trace_id | 항상 | 요청 고유 ID (UUID v4). `/feedback` 호출 시 클라이언트가 참조 |
| answer | 항상 | LLM 생성 답변 (think 태그 제거됨). Critic(기본 꺼짐) 발동 시 정정된 답변 |
| sources[].chunk_id | 항상 | Qdrant point ID — `citations[].supported_by_chunks` 매핑 키 |
| citations | claim에 ref 매핑된 게 있을 때만 | claim별 인용 매핑 — `{claim, refs(["제43조"...]), supported_by_chunks(chunk_id 리스트)}`. 클라이언트가 inline `[1][3]` UI 구성용 (Anthropic Citations API · Perplexity 패턴) |
| verification | warnings 있을 때 | 구조 검증은 **flag-only(기본)** — `{risk_level, groundedness, warnings}` 경고만 노출, 답변은 그대로. `escalation_required`는 Critic을 **켰을 때만** |
| verification.groundedness | **verifiable claim ≥ 1**일 때만 (절차형 답변에선 키 생략) | 0~1 스칼라 (`supported / verifiable`). 검증 가능한 claim(조항·숫자 추출된)만 분모로 — 평문 claim은 구조적으로 supported_by_chunks 강제 [] 라 분모에 넣으면 절차형 답변이 0점으로 깔리는 분모 결함 회피. RAGAS faithfulness · Azure AI Foundry Groundedness 패턴 |
| verification.escalation_required | Critic을 켰을 때, retrieval_gap / semantic_mismatch에서만 | `true`면 재생성 금지 판정 — 클라이언트가 재질문 유도·refusal UI로 활용 |
| crag_retries | 재검색 시만 | CRAG 재시도 횟수 (0이면 미포함) |

**Critic 재생성은 기본 꺼짐** — 실측상 hard_fail 대부분이 오탐이라 자동 교정이 무의미([design-retrospective §1.5](design-retrospective.md)). 켰을 때의 failure_type·분기 상세는 [pipeline.md](pipeline.md) 섹션 4 참조.

### 검색 결과 없음

```json
{
  "query": "...",
  "answer": "관련 내용을 찾지 못했습니다.",
  "elapsed_ms": 5000,
  "sources": []
}
```

### 에러

| 코드 | 원인 |
|------|------|
| 422 | 입력 검증 실패 |
| 500 | vLLM 연결 실패, Qdrant 연결 실패 |

### 멱등성 고려

- **준멱등**: 같은 query로 호출하면 같은 답변이 나오지만, CRAG 재시도 횟수는 검색 품질에 따라 달라질 수 있음.
- **LLM 비결정성**: `temperature > 0`이면 답변이 미세하게 달라질 수 있음. 현재 `temperature=0.0` 설정.

---

## 5. POST /embeddings

텍스트를 BGE-M3 벡터로 변환한다. 디버깅/테스트용.

### Request

```json
{
  "texts": ["보험금 지급 조건", "계약 해지 방법"]
}
```

| 필드 | 타입 | 필수 | 제한 | 설명 |
|------|------|------|------|------|
| texts | list[str] | O | max 100개 | 임베딩할 텍스트 목록 |

### Response (200)

```json
{
  "total": 2,
  "dimension": 1024,
  "vectors": [
    [0.0123, -0.0456, ..., 0.0789],
    [0.0321, -0.0654, ..., 0.0987]
  ]
}
```

### 에러

| 코드 | 원인 |
|------|------|
| 422 | texts 누락, 100개 초과 |
| 500 | BGE-M3 모델 로드 실패 |

---

## 6. POST /feedback

`/answer` · `/retrieve`의 응답에 포함된 `trace_id`를 받아 사용자 피드백을 수집한다. 서빙 trace JSONL과 `trace_id`로 조인해서 품질 신호로 사용. 엔드포인트는 서빙 경로와 **완전히 분리** — 실패해도 `/answer`에 영향 없음.

### Request

```json
{
  "trace_id": "abc-123-def-456",
  "signal": "down",
  "free_text": "근거 조항이 틀림"
}
```

| 필드 | 타입 | 필수 | 제한 | 설명 |
|------|------|------|------|------|
| trace_id | string | O | 1~64자 | `/answer`·`/retrieve` 응답의 `trace_id` 그대로 |
| signal | enum | O | `up` / `down` / `reformulated` | 사용자 만족 시그널 |
| free_text | string | X | max 2000 | 선택적 자유 서술 |

### Response (200)

```json
{
  "id": 42,
  "stored_at": "2026-04-24T01:30:00.123456"
}
```

### 에러

| 코드 | 원인 |
|------|------|
| 422 | signal이 3종 외 값, trace_id 길이 초과 |
| 503 | `FEEDBACK_ENABLED=false` 환경변수로 비활성화 시 |
| 500 | DB 장애 |

### 설계 특성

- **Insert-only**: 수정·삭제 없음 (CQRS write-only)
- **외래키 없음**: `trace_id`가 파일 기반(JSONL)이라 DB FK 불가 + trace가 아직 파일에 쓰이기 전 도착 가능 (BackgroundTasks race)
- **trace_id 실존 검증 안 함**: 엔드포인트 지연 회피. 매칭률은 집계 시점에 [scripts/trace_summary.py](../scripts/trace_summary.py) `--feedback`이 모니터링 (정상 ≥ 95%)
- **Feature flag**: `FEEDBACK_ENABLED` 환경변수로 점진적 롤아웃·즉시 비활성화 가능

### Synthetic feedback (현재 단계)

실제 사용자 UI가 없는 초기 단계에서는 `scripts/eval_ragas.py --submit-feedback`이 RAGAS Faithfulness 점수를 signal로 자동 매핑해 제출:

| Faithfulness | signal |
|---|---|
| ≥ 0.7 | up |
| 0.4 ≤ ... < 0.7 | reformulated |
| < 0.4 | down |

`free_text`에 `"synthetic from RAGAS faithfulness=0.XXX"` 명시해 실사용자 데이터와 구분. UI 통합 시 이 proxy 레이어만 교체.

### 클라이언트 호출 예시

```javascript
// 답변 받음
const data = await fetch('/api/v1/docs-rag/answer', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({query: '보험금 지급 조건?', service_code: '01'})
}).then(r => r.json());

const traceId = data.trace_id;  // 저장

// 사용자가 👎 클릭
await fetch('/api/v1/docs-rag/feedback', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    trace_id: traceId,
    signal: 'down',
    free_text: '근거가 부족함'
  })
});
```

---

## 공통 에러 응답

```json
{
  "detail": "에러 메시지"
}
```

| 코드 | 의미 |
|------|------|
| 400 | 잘못된 요청 |
| 404 | 리소스 없음 |
| 422 | 입력 검증 실패 (Pydantic) |
| 500 | 서버 내부 오류 (error_id 포함) |

### 422 상세 (Pydantic 자동 생성)

```json
{
  "detail": [
    {
      "type": "string_too_long",
      "loc": ["body", "query"],
      "msg": "String should have at most 2000 characters",
      "input": "..."
    }
  ]
}
```

---

## 필터링 동작

`/retrieve`와 `/answer`에서 사용하는 필터 조합:

| 필터 | Qdrant 조건 | 동작 |
|------|------------|------|
| service_code | MUST match | 해당 서비스 문서만 |
| document_id | MUST match | 해당 문서만 |
| start_page | MUST range gte | 시작 페이지 이상 |
| end_page | MUST range lte | 끝 페이지 이하 |
| include_keywords | MUST match_text (AND) | 모든 키워드 포함 |
| exclude_keywords | MUST_NOT match_text | 키워드 제외 |

필터 미지정 시 전체 컬렉션 대상 검색.

---

## 라우팅 전략

query 내용에 따라 자동 분류:

| query_type | 검색 전략 | 프롬프트 | 예시 |
|------------|----------|---------|------|
| structured_lookup | BM25 heavy (x3/x8) | 원문 인용 | "제43조", "별표 1", "Section 4" |
| interpretation | Dense heavy (x8/x3) | IRAC 구조 | "무면허운전 시 보장되나요?" |
| procedure | Dense heavy (x8/x3) | 단계별 설명 | "보험금 청구 방법" |
| comparison | Dense heavy (x8/x3) + Query Decomposition | 비교표 | "1종과 2종 차이" |
| simple_fact | Hybrid (x6/x6) | 간결 답변 | "보험금 지급 기준" |
