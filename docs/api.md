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
| POST | `/payout` | 결정론 지급 질의 (SQL 경로) | O |
| POST | `/terms` | 결정론 계약조건 질의 (청약철회·갱신) | O |
| POST | `/coverage` | 결정론 보장판정 (별표3 ICD 3-값) | O |
| POST | `/exclusion` | 결정론 면책 상세 (지급 제외 사유) | O |
| POST | `/catalog` | 결정론 담보 멤버십 (이 상품에 X 담보 있어?) | O |
| POST | `/waiting` | 결정론 면책기간·감액 (언제부터 온전히 받나?) | O |
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
    "warnings": ["인용 조항이 검색 근거에 없음(검색 격차 가능): 제99조"]
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

## 5. POST /payout

**결정론 SQL 경로** — "얼마·언제"처럼 값이 정해진 질의를 `payout_rule` 테이블에서 **결정론**으로 집어온다. RAG(`/answer`)와 분리된 **사이드카**: 담보를 못 짚거나 규칙이 안 맞으면 `matched=false` + RAG 폴백 신호(precision-first — 억지 지급률 대신 RAG). 서빙 로직 [`rag/payout_sql.py`](../src/v1/rag/payout_sql.py), 데이터 `PayoutRepository`(payout_rule). 라우터 통합(질의 유형 감지 후 SQL/RAG 자동 분기)은 후속(로드맵 B5).

### Request

```json
{
  "query": "중환자실 입원하면 하루 얼마 받아요?",
  "service_code": "01",
  "product_id": "LINA_ICU_2024"
}
```

| 필드 | 타입 | 필수 | 제한 | 설명 |
|------|------|------|------|------|
| query | string | O | max 2000 | 지급 관련 질의 |
| service_code | string | X | max 10 | 서비스 필터 |
| product_id | string | X | max 64 | 특정 상품 한정 (미지정 시 전 상품) |

### Response (200)

```json
{
  "query": "중환자실 입원하면 하루 얼마 받아요?",
  "route": "sql",
  "matched": true,
  "answer": "중환자실 입원급여금 → 가입금액의 1일당 1% (한도 10일) ※1년이내 재해외 시 50% 감액  ※ 지급 제외(면책): 고의 등(제7조) 확인 필요",
  "rule": {
    "product_id": "LINA_ICU_2024", "coverage": "중환자실 입원급여금",
    "rate_pct": 1, "per_unit": "1일당", "limit_days": 10,
    "reduction_rate_pct": 50, "reduction_period": "1년이내", "reduction_cause": "재해외"
  },
  "exclusions": [{"jo": 7, "title": "보험금을 지급하지 않는 사유", "body": "..."}]
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| route | 항상 | `"sql"` — 3경로 라우터의 SQL 경로 표식 |
| matched | 항상 | 결정론 답을 냈나. `false`면 RAG(`/answer`)로 폴백하라는 신호 |
| answer | 항상 | 결정론 답변 한 줄 + **면책 강제첨부**("얼마?"에 지급 제외를 항상 붙임 — 지급률만 답하면 소비자 손해). `matched=false`면 `"관련 지급규칙을 찾지 못했습니다(→RAG)."` |
| rule | matched=true일 때만 | 근거 `payout_rule` row (coverage·rate_pct·한도·감액). miss면 `null` |
| exclusions | matched=true일 때만 | 강제첨부된 상품 general 면책 조 `[{jo, title, body}]`(kind='general', `coverage_exclusion_map ⋈ clause`). answer엔 body에서 뽑은 실제 사유 태그(고의·전쟁내란 등)를 노출. 감액(reduction)은 payout에 이미 포함 |

### 에러

| 코드 | 원인 |
|------|------|
| 422 | query 누락, 길이 초과 |
| 500 | DB 연결 실패, payout_rule 미존재 |

### 클라이언트 패턴 (SQL-first → RAG 폴백)

```javascript
let r = await post('/payout', {query, service_code: '01'});
if (!r.matched) r = await post('/answer', {query, service_code: '01'});  // 결정론 miss → RAG
```

---

## 6. POST /terms

**결정론 계약조건 경로** — "언제까지?"(청약철회·갱신)를 `product`에서 결정론으로. **준용 NULL 철학**: 특약은 청약철회가 NULL이 정답(보통약관 준용 소관) — 억지 값 대신 *"제19조 준용 소관, 주계약 미확보로 확인 필요"*(precision-first, "확신에 찬 오답" 0). 상품 미해소(담보 키워드 없음)면 `matched=false`→RAG. 로직 [`rag/terms_sql.py`](../src/v1/rag/terms_sql.py), 데이터 `product`(`load_terms.py --load`).

### Request

```json
{ "query": "중환자실 특약 청약철회 언제까지 가능한가요?", "service_code": "01", "product_id": "LINA_ICU_2024" }
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| query | string | O | 계약조건 질의 (청약철회·갱신·만기) |
| product_id | string | X | 특정 상품 한정. 미지정 시 query의 담보 키워드로 해소 |

### Response (200)

```json
{
  "query": "중환자실 특약 청약철회 언제까지?",
  "route": "sql",
  "matched": true,
  "answer": "갱신형 · 청약철회: 제19조 준용 소관 — 주계약 미확보로 확인 필요",
  "product": {"product_id": "LINA_ICU_2024", "is_renewable": true, "cooling_off_days": null, "resolution_note": "…준용(제19조) 소관…"}
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| answer | 항상 | 갱신 여부(+주기)·만기·청약철회(실값 "N일 이내" 또는 특약이면 **준용 소관 확인 필요**). 예: "갱신형(10년 주기) · 10년 만기 · 청약철회 15일 이내". miss면 `"…(→RAG)."` |
| product | matched=true일 때만 | 근거 (is_renewable·renewal_cycle_years·term_years·cooling_off_days·resolution_note) |

---

## 7. POST /coverage

**결정론 보장판정 경로** — "이 병(코드) 보장돼요?"를 별표3 ICD 코드범위로 **3-값 판정**(보장 / 미보장→실제 담보 리다이렉트 / 판정불가). 억지 판정 안 함(판정불가 = precision-first). 담보 특정성 반영 — 같은 코드도 담보 따라 갈림(D05는 암진단자금엔 **미보장**, 제자리암진단자금엔 **보장**). **정합 조립(reconcile)**: 판정에 실제 지급 담보의 payout을 붙여 "얼마+보장"의 **모순 없는 완결 답** — 미보장이면 리다이렉트 담보의 지급률까지("암진단자금 미보장 → 제자리암진단자금 10%"). **병명→코드는 별도 계층** — 코드 미특정이면 `matched=false`→RAG. 로직 [`rag/coverage_sql.py`](../src/v1/rag/coverage_sql.py), 데이터 `coverage_range`(`load_coverage.py --load`).

### Request

```json
{ "query": "C50 유방암은 보장되나요?", "service_code": "01", "product_id": "DIRECT_INPT_2024" }
```

### Response (200)

```json
{
  "query": "D05는 암진단자금으로 보장돼요?",
  "route": "sql",
  "matched": true,
  "answer": "D05 → 미보장 (암진단자금) → 실제 담보: 제자리암진단자금. … ※ 제자리암진단자금 지급: 가입금액의 10%",
  "code": "D05",
  "verdict": {"verdict": "미보장", "coverage": "암진단자금", "redirect_coverage": "제자리암진단자금", "evidence": "…"}
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| code | 항상 | 질의에서 특정한 ICD 코드(명시 우선, 소형 병명맵). 못 짚으면 `null`→matched=false |
| verdict | matched=true | `{verdict(보장/미보장/판정불가), coverage, redirect_coverage, evidence}` |

---

## 8. POST /exclusion

**결정론 면책 상세 경로** — "뭐가 면책이야?(지급 안 되는 사유)"를 면책 조에서 실제 사유(고의·전쟁내란·위험활동 등)로 나열. payout의 **강제첨부**(모든 지급 답에 항상 붙임)와 달리, 면책만 묻는 **단독 질의**의 결정론 답. **준용 완결성**: 특약은 고유 면책(고의)만 자체 보유하고 공통면책(전쟁·임신·위험활동)은 주계약 준용 소관 — 주계약 미확보면 *"공통면책은 제19조 준용 소관, 미확보로 확인 필요"*를 붙여 **완결(빠짐 없음)·정직(없는 걸 안전하다 안 함)**. 복합약관(자체 완비)은 준용 노트 없음. 상품은 담보 키워드로 해소(`product_id`도 가능) — 못 짚으면 `matched=false`→RAG. 로직 [`rag/exclusion_sql.py`](../src/v1/rag/exclusion_sql.py).

### Request

```json
{ "query": "중환자실 특약은 뭐가 면책인가요?", "service_code": "01", "product_id": "LINA_ICU_2024" }
```

### Response (200)

```json
{
  "query": "중환자실 특약은 뭐가 면책인가요?",
  "route": "sql",
  "matched": true,
  "answer": "지급 제외(면책) 사유: 고의 등 (제7조) — 상세는 해당 조 확인 필요  ※ 공통면책(전쟁·임신·위험활동 등)은 제19조 준용 소관 — 주계약 미확보로 확인 필요",
  "exclusions": [{"jo": 7, "title": "보험금을 지급하지 않는 사유", "body": "…"}]
}
```

---

## 8-1. POST /catalog

**결정론 담보 멤버십 경로** — "이 상품에 X 담보 있어? / 뭐 보장해?"를 **특약 목록**에서 결정론으로. coverage(별표3 ICD "이 코드 보장돼?")·payout("얼마")와 다른 축 = **담보 존재 여부**(가장 흔한 보장 질문). KB 복합약관은 특약 1개=담보 1개라 특약 목록이 곧 담보 catalog(`CoverageRepository.list_catalog`). 질의의 담보가 catalog에 있으면 "있음" 확정, **못 찾으면 부재를 단정하지 않고** `matched=false`→RAG(동의어·다른 표기로 있을 수 있어 "없다"는 확신에 찬 오답). 상품은 브랜드 키워드로 해소(`resolve_base_product_id`, `product_id`도 가능). 로직 [`rag/catalog_sql.py`](../src/v1/rag/catalog_sql.py).

### Request

```json
{ "query": "골든라이프에 파킨슨병진단비 담보 있어?", "service_code": "01", "product_id": "KB_GOLDENLIFE_2026" }
```

### Response (200)

```json
{
  "query": "골든라이프에 파킨슨병진단비 담보 있어?",
  "route": "sql",
  "matched": true,
  "answer": "네, 보장 담보에 있습니다: 파킨슨병진단비 (근거: 해당 특약)",
  "covered": ["파킨슨병진단비"],
  "catalog_size": 69
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| covered | 항상 | 질의에서 확정한 담보명(정규화). 미적중이면 `[]`→matched=false→RAG |
| catalog_size | 항상 | 해당 base 상품의 담보(특약) 총수 — 근거 |

---

## 8-2. POST /waiting

**결정론 면책기간·감액 경로** — "언제부터 (온전히) 받나? / 면책기간·감액은?"을 담보(특약)별로. KB 간편건강보험은 기저 지급액이 대부분 가입금액(소비자 설정)이라 지급률은 결정론 불가(→RAG)지만, **면책기간(가입 후 N일 보장 제외)·감액(가입 후 1년간 M% 지급)은 2열 표에 명시**돼 정밀 추출된다. 특약 1개=담보 1개라 질의의 담보명으로 특약을 짚어 `product.waiting_period_days` + 감액(`payout_rule` source='kb_table')을 답한다. 브랜드+담보 미해소·데이터 없으면 `matched=false`→RAG. 담보명 로마자 접미(질병사망**Ⅲ**)는 유일할 때만 매칭(암수술비Ⅰ vs Ⅱ 모호는 RAG). 로직 [`rag/waiting_sql.py`](../src/v1/rag/waiting_sql.py).

### Request

```json
{ "query": "골든라이프 암진단비 면책기간 얼마야?", "service_code": "01", "product_id": "KB_GOLDENLIFE_2026" }
```

### Response (200)

```json
{
  "query": "골든라이프 암진단비 면책기간 얼마야?",
  "route": "sql",
  "matched": true,
  "answer": "암진단비 · 면책기간 90일(가입 후 90일간 보장 제외) · 가입 후 1년이내 50% 감액",
  "fact": {"product_name": "…암진단비(유사암제외)(간편가입)", "waiting_period_days": 90, "reduction_period": "1년이내", "reduction_rate_pct": 50}
}
```

---

## 9. POST /embeddings

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

## 10. POST /feedback

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
| comparison | Dense heavy (x8/x3) | 비교표 | "1종과 2종 차이" |
| simple_fact | Hybrid (x6/x6) | 간결 답변 | "보험금 지급 기준" |

**SQL 자동 라우팅 (3경로 중 SQL 경로)**: 위 RAG 분류 전에, `/answer`는 결정론 질의를 감지하면 관계형 테이블에서 값을 집어와 LLM 없이 답한다(응답 `route.strategy="sql"`). 네 갈래(순서대로 검사) — ①**"얼마·지급률"**(`is_payout_amount_query`)→`payout_rule`(+면책 강제첨부), ②**"언제까지·청약철회·갱신"**(`is_terms_query`+담보 해소)→`product`(준용 NULL), ③**"이 병 보장돼요?"**(`is_coverage_query`+ICD 코드)→`coverage_range`(별표3 3-값+reconcile payout), ④**"뭐가 면책?"**(`is_exclusion_query`+담보 해소)→면책 사유 나열. 게이트 + 매칭(값 특정)이 **둘 다** 성립할 때만 발동, 아니면 위 RAG 경로 그대로(precision-first 2중 안전). `SQL_ROUTE_ENABLED=false`로 끔. 라우팅 회귀는 `make eval-sql-routing`(16문항 accuracy 1.0)로 고정. 결정론 계층 상세는 [domain-model.md](domain-model.md)·[eval-and-golden.md](eval-and-golden.md).
