# CLAUDE.md

## 프로젝트 개요
PDF 문서를 처리하는 RAG 파이프라인.
PDF 등록 → Celery 비동기 `extract → ocr → chunk → embed` → Qdrant 하이브리드 검색 →
쿼리 라우팅 → CRAG 루프 → 프롬프트 분기 → vLLM 답변 생성 → Self-RAG 검증.

## 기술 스택
- Python 3.10, FastAPI, uv, Celery + RabbitMQ
- BGE-M3 1024차원 (임베딩), BGE-Reranker-v2-m3 (리랭킹), Qdrant (벡터DB, INT8 양자화)
- PostgreSQL (메타), vLLM + Qwen3 (LLM — 로컬 8GB는 Qwen3-4B-AWQ, 평가는 14B 구성), PaddleOCR PP-StructureV3 (layout+table+formula+OCR)
- Docker Compose, 로컬 RTX 4060 Laptop 8GB · WSL2(Ubuntu) · 16-core (평가 수치는 대형 GPU + Qwen3-14B 구성)

## 디렉토리 구조
- `src/v1/router.py`    : FastAPI 엔드포인트 정의 + 의존성 주입 + PII guard (얇은 진입점)
- `src/v1/task/`        : Celery 태스크 (extract, ocr, chunk, embed)
- `src/v1/rag/`         : RAG 서빙 전략 — 쿼리 라우팅·검색·리랭킹·sibling·토큰 예산·검증·critic·trace·prompts
  - `clients.py` (Qdrant·LLM·CrossEncoder 싱글톤) / `search.py` (filter·hybrid·rerank·decompose)
  - `sibling.py` / `tokens.py` / `classifier.py` / `grader.py` / `prompts.py` / `trace.py`
- `src/v1/guards/`      : Input Guard (PII 정규식 마스킹) — Guardrails 6계층 중 1계층
- `src/v1/utils/`       : 데이터 파이프라인 유틸 (청킹, 전처리, 임베딩, OCR 래퍼)
- `src/v1/config/`      : 설정 (DB, Qdrant, LLM, 검색/청킹/OCR 상수)
- `odl/`                : PDF→Markdown 변환 (별도 Docker, Java + docling-fast hybrid)
- `paddle/`             : 이미지 OCR (PP-StructureV3 layout+table+formula+OCR, 별도 Docker, CPU 모드)
- `db/schema.sql`       : DDL 8테이블 + 초기 데이터
- `scripts/`            : 평가·관측 스크립트 (RAGAS, OCR, 인덱스 헬스, 서빙 trace 집계, DoD smoke test, feedback 주간 집계)
- `tests/rag/`          : grader/critic/feedback/trace 단위 테스트
- `tests/guards/`       : PII 마스킹 단위 테스트
- 통합 실행: `uv run pytest tests/ -v` (integration 마크 자동 skip)

## 자주 쓰는 명령어

> 자주 쓰는 명령 alias는 [Makefile](Makefile) 참조. 아래는 각 명령의 옵션·동작 상세.

```bash
# 빌드 & 기동
docker compose build && docker compose up -d
docker compose up -d                            # .env 변경 시 (재빌드 불필요)

# 로그
docker compose logs -f api celery               # 파이프라인 + API 실시간
docker compose logs --tail=20 api               # 최근 20줄
docker compose ps                               # 서비스 상태

# DB (host에 psql 없으면 컨테이너 경유)
cat db/schema.sql | docker compose exec -T postgres psql -U lawuser -d ai_parser
docker compose exec postgres psql -U lawuser -d ai_parser -c "\d tb_query_feedback"

# 단위 테스트 (integration 마크는 자동 skip)
uv run pytest tests/ -v                                              # rag + guards 단위 (host)
docker compose exec api uv run pytest tests/ -v -m integration       # critic E2E (mocked LLM/Qdrant, docker 안)

# 평가·관측 (Tier 1 일상용)
uv run python scripts/smoke_test.py       # 10 DoD 자동 검증 (critic 필드 포함)
uv run python scripts/trace_summary.py                 # 서빙 trace 12-섹션 집계 (critic + input_guard 포함)
uv run python scripts/trace_summary.py --feedback      # 위 + Feedback DB 7일 JOIN 섹션 추가
OPENAI_API_KEY=sk-... uv run python scripts/eval_ragas.py --submit-feedback   # RAGAS 평가(GPT-4o-mini judge) + synthetic feedback 자동 제출
```

## 서비스 구성 (docker compose ps)
| 서비스 | 포트 | GPU | 역할 |
|--------|------|-----|------|
| api | 8002 | 0 | FastAPI (검색/RAG) + BGE-M3/Reranker |
| celery | - | 0 | Celery Worker (extract→ocr→chunk→embed) + BGE-M3/Reranker |
| flower | 5555 | - | Celery 모니터링 UI |
| vllm | 8000 | 0 | Qwen3-4B-AWQ (KV fp8, 8GB 프로파일) — OpenAI 호환 API로 교체 가능 |
| paddle | 5003 | **CPU** | PP-StructureV3 (Blackwell sm_120 미지원으로 CPU 고정) |
| odl | 5002 | - | PDF→Markdown 변환 (FastAPI 래퍼:5002 + docling-fast:5010) |
| rabbitmq | 5672/15672 | - | 메시지 큐 |
| postgres | 5432 | - | PostgreSQL (compose 내부 volume) |
| qdrant | 6333/6334 | 0 | Qdrant 벡터DB (GPU 인덱싱) |

**GPU 배치 정책**: 현재 모든 GPU 의존 서비스가 GPU 0에 통합. 1~3번은 비어있음 (향후 워크로드 분산 또는 paddle GPU 복귀 시 활용). vLLM은 `--gpu-memory-utilization 0.30`으로 약 29GB만 예약.

## 상태 흐름
```
00(대기) → 22(PDF추출중) → 21(추출완료) → 24(OCR중) → 23(OCR완료)
→ 32(청킹중) → 31(청킹완료) → 42(임베딩중) → 43(임베딩완료) → 41(벡터DB적재) → 11(전체완료)
에러: 91(PDF추출) / 92(OCR) / 93(청킹) / 94(청킹/DB) / 95(임베딩) / 96(임베딩/벡터DB) / 99(기타)
```

## 코드 스타일
- 주석: Why > What. 환경변수: `.env`로 관리, 하드코딩 금지.
- `.env` inline 주석 금지 — Makefile `include .env` 가 trailing whitespace까지 export해서 path 오염 (분리된 줄에 주석).
- import: 표준 → 외부 → 프로젝트. 타입힌트: `X | None` (Python 3.10+).
- 패키지 `__init__.py` 는 light 모듈만 re-export. heavy 자원(GPU 모델 로딩·외부 연결) 모듈은 사용처가 submodule에서 직접 import — unit test 가 의도치 않게 모델 로드 트리거하는 회귀 방지.
- 민감 데이터(개인정보, 계약번호)는 로그/프롬프트에 넣지 않는다.

## 설계 원칙 — 검증된 것만 메인 경로에
새 검증·평가 컴포넌트(NLI judge / LLM judge / 새 verifier 등) 추가 시:
1. 한국어 보험 도메인 평가셋에서 precision/recall **측정**.
2. **precision ≥ 0.9** 임계 통과 후에만 메인 경로 도입.
3. 그 전엔 별도 sidecar 또는 `*_judge: Callable | None = None` slot 형태로만 노출.

근거: 검증 안 된 verifier를 메인 경로에 끼우면 false positive가 hard_fail rate를 폭증시켜 시스템 신뢰도가 오히려 하락. 현재 `semantic_judge` slot이 비어있는 이유 (README "검증되지 않은 영역" 섹션 참조).

## 도메인 용어

**데이터 & 상태**
- `service_code`: 서비스 구분 코드 (2자리, "01"=AI_PARSER)
- `heading_path`: 조문 계층 경로 ("제1장 일반사항 > 제4조 보험금의 지급사유")
- `chunk_type`: 청크 타입 (`text` / `table` / `image`)
- `part_index` / `part_total`: 같은 heading_path 내 분할 순번/총수 (sibling 복원용)
- `status_code`: 파이프라인 상태 코드 (00=대기, 11=완료, 91~99=에러)

**검색 & 검증**
- `rerank_score` (top-1): CrossEncoder 점수 중 최상위. CRAG threshold 비교 대상
- `CRAG_SCORE_THRESHOLD`: 재시도 발동 기준 (현재 0.3). `score_before` 이 값 미만이면 재검색
- `score_before` / `score_after`: CRAG 재시도 전후의 top-1 rerank score
- `decomposition.method`: COMPARISON 쿼리 분해 방식 (`rule` / `llm` / `llm_failed` / `none`)
- `first-wins`: `search_comparison` 이 **초기 호출에서만** decomposition 기록 — CRAG 재시도의 rewritten query가 덮어쓰지 않도록
- `risk_level`: Self-RAG 위험 등급 (`pass` / `warn` / `soft_fail` / `hard_fail`)
- `claim-근거 매핑 coverage`: `supported_claims_count / verifiable_claims_count`. **검증 가능한 claim만 분모** (조항·숫자 추출된 것). 평문 claim은 구조적으로 supported_by_chunks 강제 [] 라 분모에 넣으면 절차/해석 답변이 0%로 깔리는 분모 결함 → RAGAS faithfulness 정의와 일치

**관측**
- `TraceRecord`: 요청 1건당 JSONL 1줄. schema_version 필드 없음 — 단일 producer 환경 미니멀 정책 (consumer는 `dict.get()` 방어적 읽기)
- `trace_span`: 구간 시간 측정 context manager. CRAG 재시도 시 누적 합산
- `BackgroundTasks`: FastAPI의 응답 후 비동기 실행 메커니즘. trace write에 사용 (latency 영향 0)
- `critic.invoked / failure_type / regenerate_improved`: Critic dispatch 동작 기록. `trace_summary.py` Section 10이 집계
- `SLA`: [README 섹션  SLA 타겟](README.md) 참조. 관측 숫자 해석의 기준선

**Critic & Feedback**
- `FailureType`: `generation_error` / `retrieval_gap` / `unit_error` / `semantic_mismatch` / `minor` — `classify_failure()` 결과
- `escalation_required`: retrieval_gap·semantic_mismatch일 때만 응답에 노출. 클라이언트가 재질문 유도용
- `REGENERATE_WITH_HINT_PROMPT`: hint-guided regenerate용 프롬프트. `build_hint()` 출력을 system에 주입
- `trace_id`: UUID v4. `/answer`·`/retrieve` 응답에 포함. `/feedback`·DB·JSONL trace 연결 키
- `signal` (feedback): `up` / `down` / `reformulated` (Literal + DB CHECK)
- Synthetic feedback proxy: `eval_ragas.py --submit-feedback`이 Faithfulness 점수를 signal로 매핑 (≥0.7→up / ≥0.4→reformulated / <0.4→down)

## 주의사항 — 연쇄 수정
- **DB 스키마**: `schema.sql` + `models.py` + `repository.py` + task 파일 동시 수정
- **상태 관리 CQRS**: `tb_document_status_log`(원본, append-only) + `tb_document_status`(읽기용). `update_status()`가 둘 다 처리
- **Qdrant payload**: `embed.py` payload + `router.py` 검색/sibling + 컬렉션 재생성
- **Celery 체인**: `prev_result = {"service_code", "document_id", "document_name"}` 고정
- **BM25 이름**: `content-bm25`가 `qdrant.py`/`embed.py`/`router.py` 세 곳. 변경 시 컬렉션 재생성
- **벡터 1024차원**: `qdrant.py` + BGE-M3. 모델 변경 시 반드시 일치 확인
- **Qdrant point ID 타입**: Qdrant가 unsigned integer 또는 UUID만 허용 — string ID는 거절. `embed.py` `PointStruct(id=int)` + `tb_document_contents.qdrant_point_id` BIGINT + `repository.get_by_qdrant_id(int)` 셋이 모두 정수로 일관. chunks.id (BIGSERIAL) 그대로 사용. 이 정책 깨면 검색 → DB lookup 시 silent mismatch 발생
- **페이지 마커**: `<!-- page:N -->`가 `extract.py`/`preprocess.py`/`odl/server.py` 세 곳
- **ODL 2프로세스**: `odl/Dockerfile` CMD에서 docling-fast(:5010) + FastAPI 래퍼(:5002) 동시 실행. `extract.py`의 `hybrid_url`과 포트 일치 필수
- **TraceRecord 신규 필드 추가**: 항상 optional (`X | None = None`) + `trace_summary.py` aggregator/renderer 등록 + `smoke_test.py` step 추가. consumer는 `dict.get()` 으로 방어적 읽기 (schema_version 필드 없으니 버전 분기 불가)
- **Input Guard (PII + Injection)**: `guards/pii.py` `_PII_PATTERNS` (5종) + `guards/injection.py` `_INJECTION_PATTERNS` + `_ZERO_WIDTH_RE` + `guards/__init__.py` export + `router.py` `_apply_input_guard` (PII 마스킹 → injection sanitize → 양쪽 진입점 2곳에서 호출) + `trace.py` `input_guard` 필드 (`pii_found` / `injection_threats` 키) + `trace_summary.py` Section 11 (PII + Injection 통합 통계) + `smoke_test.py` step 11 — 총 7곳 동시 수정. 신규 패턴 추가 시 단위 테스트(`tests/guards/test_pii.py` / `test_injection.py`) 동시 갱신
- **Output Guard (leak + profanity)**: `guards/output.py` `_LEAK_PATTERNS`·`_PROFANITY_PATTERNS` + `guards/__init__.py` export + `router.py` answer LLM 호출 직후 + critic regenerate 후 양쪽 sanitize_output 호출 (threats 누적) + `trace.py` `output_guard` 필드 + `trace_summary.py` Section 12 (`_aggregate_output_guard` + `_render_output_guard` + AGGREGATORS/RENDERERS 등록) + `tests/guards/test_output.py` — 총 6곳. leak은 silent 제거, 욕설은 라벨만 (PII 마스킹과 동일 정책)
- **Citation 응답 노출**: `rag/grader.py` `verify_answer["claims"][i]["supported_by_chunks"]`(이미 산출됨) + `rag/search.py` `format_sources`에 `chunk_id` 키 + `router.py` answer endpoint citations projection (claim별 ref + supported_by_chunks 매핑, no_refs claim 제외) + `docs/api.md` 응답 스키마 — 총 4곳. 데이터는 verify_answer가 매 요청 자동 산출, projection만 추가
- **Groundedness Score**: `rag/grader.py` claim 단위 `supported_by_chunks` 산출(기존) + `router.py` `_verification_summary` 헬퍼 (supported / **verifiable** ratio = 0~1, verifiable=0이면 키 생략) + `rag/trace.py` `verification.groundedness` 필드 + `trace_summary.py` `_aggregate_verification` percentile + `_aggregate_provenance` coverage_pct도 verifiable 분모 + `_render_verification` 출력 + `docs/api.md`·`docs/pipeline.md` 응답 스키마 + `tests/rag/test_trace_record.py` 두 케이스(verifiable≥1 / verifiable=0) — 총 7곳. RAGAS faithfulness 패턴 — 평문 claim 분모 결함 회피가 핵심
- **Critic dispatch**: `router.py` `answer()` (verify_answer 직후 분기) + `rag/grader.py` `classify_failure`/`build_hint` + `rag/prompts.py` `REGENERATE_WITH_HINT_PROMPT` + `rag/__init__.py` exports — 신규 failure type 추가 시 4곳 모두 갱신. **Feature flag**: `CRITIC_DISPATCH_ENABLED` **기본 false(필요 시 켬)** — 실측 hard_fail 6/6 오탐이라 auto-regenerate 무의미(design-retrospective §1.5). 필요 시 true.
- **Feedback endpoint**: `schema.sql` (tb_query_feedback) + `models.py` (QueryFeedback) + `repository.py` (FeedbackRepository) + `schemas.py` (FeedbackRequest/Response) + `router.py` (/feedback endpoint + `trace_id` 응답 노출 양쪽: `/answer`·`/retrieve`) + `eval_ragas.py` (synthetic feedback 매핑) + `trace_summary.py --feedback` (집계) — 총 7곳 동시 수정. `trace_id` 누락 시 클라이언트가 참조 못 함
- **Synthetic feedback 매핑 임계값**: `eval_ragas.py` `map_score_to_signal` (0.7/0.4 경계). 변경 시 과거 데이터의 signal 분포가 달라지므로 `trace_summary.py --feedback` 집계 해석도 같이 재검토

## 주의사항 — 성능/설정
- **설정 일원화**: `SIBLING_WINDOW`, `CRAG_SCORE_THRESHOLD`, `CRAG_MAX_RETRIES`, `SEARCH_PREFETCH_MULTIPLIER` → `config/settings.py`. 하드코딩 금지.
- **CrossEncoder 1회**: 비교 질문은 `search_rrf_only` 로 수집, 리랭킹 1회만 (`search_comparison`).
- **Sibling 배치**: `_fetch_siblings_batch()`로 OR 필터 1회 조회. N+1 금지.
- **Qdrant upsert**: 1000개 단위. 32MB HTTP 제한의 ~19%.
- **OCR 병렬**: `ThreadPoolExecutor(4)`로 paddle HTTP 병렬화.
- **트랜잭션**: repository는 `flush()`만, task에서 `db.commit()`. delete+insert 원자성 보장.

## 주의사항 — 환경/경로
- `CHUNKER_TYPE` 미설정 → `chunk.py` `KeyError`. `.env` 필수.
- 경로 3종: 호스트 `./data` → 워커 `/app/data` → ODL·Paddle `/data`.
- ODL 파일 UID 1000 → 워커에서 직접 삭제 불가 → `/cleanup` API.
- **PDF 추출 전략**: ≤200p → docling-fast 시도 → 품질 미달 시 Java fallback. >200p → Java-direct. `DOCLING_PAGE_LIMIT`로 제어.
- DB 커넥션: API는 `get_db()`, Celery는 `task_session()`.

## 관측 (Tracing)
- 모든 `/retrieve`·`/answer` 요청은 `data/eval/trace/YYYYMMDD/traces.jsonl`에 1줄씩 append (BackgroundTasks 비동기)
- 스키마는 [src/v1/rag/trace.py](src/v1/rag/trace.py)의 `TraceRecord` dataclass
- 응답 `verification` 필드는 `{risk_level, warnings, escalation_required?}`. claim·provenance·critic 상세는 trace 전용 (응답 slim, 관측 풍부)
- 집계: `scripts/trace_summary.py` (12 섹션, `--feedback` 플래그로 Feedback DB JOIN 13번째 섹션 추가) / 검증: `scripts/smoke_test.py` (11 step)

## 세부 가이드
- @docs/api.md          : REST API 설계 (엔드포인트, 스키마, 에러 코드, 멱등성)
- @docs/architecture.md : 시스템 구성도, 데이터 흐름, 성능 수치, 장애 대응
- @docs/pipeline.md     : RAG 서빙 (쿼리 라우팅, Query Decomposition, CRAG, 프롬프트, Self-RAG)
- @docs/chunking.md     : 청킹 전략 (adaptive/fixed, OCR 파이프라인 3단계 필터, sibling 복원)