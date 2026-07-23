# 📄 docs-rag

> **Production-grade Korean RAG pipeline for structured PDFs**
>
> 📓 [Notion — 설계 근거](https://www.notion.so/DocsRAG-31b9fb2de50b80b59e04d05d8985ceca)

---

PDF 문서를 처리하는 범용 RAG 파이프라인 — 약관·법령·매뉴얼·제품 문서 등 한국어 구조화 문서에 공통 적용.
PDF 등록 → 비동기 `extract → ocr → chunk → embed` → Qdrant 하이브리드 검색 → Adaptive 라우팅 → CRAG 루프 → 프롬프트 분기 → vLLM 답변 생성 → Self-RAG 검증 → 서빙 trace 기록.

현재 운영 corpus는 보험·법령이지만, 도메인 비종속 설계 — routing 정규식 / 프롬프트 템플릿만 바꾸면 다른 도메인에 재사용 가능.

> **English** — Production-grade Korean RAG pipeline for structured documents (insurance policies, regulations, manuals). Hybrid search (BGE-M3 + BM25 + RRF), CRAG retrieval gate, Self-RAG verification, and **critic-guided regeneration** with 5-class failure-type classification. Includes 12-section serving-trace observability, feedback endpoint with trace-id join, and 4-layer guardrails (PII / Injection / Grounding / Output). Honest SLA reporting — known gaps are documented (see *Evaluation snapshot* below).

## 파이프라인 한눈에 보기

PDF 한 건이 등록되면 **수집(비동기 4-스테이지)** → **서빙(Adaptive RAG)** 두 흐름을 거친다. 수집은 Celery 체인으로 단계마다 상태 코드를 남겨 **실패 지점부터만 재처리**하고, 서빙은 쿼리 성격에 맞춰 검색·검증 경로를 분기한다.

```mermaid
flowchart TD
    subgraph 수집["📥 수집 — Celery 비동기 체인 (상태코드 기반 재처리)"]
        A[PDF 입력] --> B["extract<br/>ODL: docling-fast → Java fallback<br/>PDF → Markdown + 이미지"]
        B --> C["ocr<br/>PaddleOCR PP-StructureV3<br/>이미지·표 → 구조화 텍스트"]
        C --> D["chunk<br/>정규화 + 룰베이스 청킹<br/>heading 경계 · 참조 추적 · sibling"]
        D --> E[("PostgreSQL<br/>청크 원본 + 계층·참조 메타")]
        E --> F["embed<br/>BGE-M3 1024d · Cosine · INT8"]
        F --> G[("Qdrant<br/>Dense + BM25")]
    end
    subgraph 서빙["🔍 서빙 — POST /answer (Adaptive RAG)"]
        Q[사용자 질의] --> R["Adaptive 라우팅<br/>5-type 분류 + Dense/BM25 배수"]
        R --> S["하이브리드 검색<br/>Dense + BM25 → RRF → Reranker"]
        S --> T{"CRAG<br/>rerank score ≥ 0.3?"}
        T -->|No · 쿼리 재작성| R
        T -->|Yes| U["프롬프트 분기 → vLLM Qwen3-14B"]
        U --> V["Self-RAG 검증<br/>조항·수치 대조 → risk_level"]
        V --> W["Critic<br/>failure_type 5분류 → 조건부 regenerate"]
        W --> X[답변 + trace_id + citations]
    end
    G -.검색 대상.-> S
```

### 수집 4-스테이지 (PDF 등록 → 벡터 적재)

| 스테이지 | 하는 일 | 핵심 설계 | 상세 |
|---|---|---|---|
| **① extract** | ODL(`opendataloader-pdf`)로 PDF → Markdown + 내부 이미지 추출 | ≤200p `docling-fast`(hybrid ML) 시도 → 품질 검증 실패 시 Java fallback / >200p Java-direct (`DOCLING_PAGE_LIMIT`). XY-Cut++ 읽기 순서·구조 보존(heading/paragraph/table/list/caption/image), 헤더·푸터·워터마크 자동 제거, **로컬 실행·데이터 유출 0%** | [architecture.md](docs/architecture.md) |
| **② ocr** | 삽입·스캔 이미지를 PaddleOCR로 구조화 | PP-StructureV3(layout+table+formula+OCR). `is_valid_image` **6단계 입구 필터**로 garbage(1px·아이콘·단색·가로띠) 컷 → 표는 그리드로 복원, 나머지는 image 청크. Blackwell sm_120 미지원으로 현재 **CPU 고정** | [chunking.md](docs/chunking.md) |
| **③ chunk** | 텍스트 정규화 + 룰베이스 청킹 + OCR 청크 합류 | **경계 3원칙** — (1) heading → 새 청크 시작, (2) paragraph/table/list 의미 단위 보존, (3) 조항 번호 패턴 → 참조 관계 기록. Adaptive(헤딩 트리 · text/table/image 분리 · `part_index` sibling 복원) / Fixed(800자 · 150 오버랩) 전략 선택 | [chunking.md](docs/chunking.md) |
| **④ embed** | 청크 → BGE-M3 → Qdrant 적재 | 1024차원 · Cosine · INT8 양자화(rescore + oversampling). Dense와 함께 BM25 벡터(`content-bm25`) 병행 저장, 1000개 배치 upsert. Reranker는 서빙 시 `bge-reranker-v2-m3` | [architecture.md](docs/architecture.md) |

### 상태 코드 — 실패 지점부터 재처리

```
00(대기) → 22(추출중) → 21(추출완료) → 24(OCR중) → 23(OCR완료)
→ 32(청킹중) → 31(청킹완료) → 42(임베딩중) → 43(임베딩완료) → 41(벡터DB적재) → 11(완료)
에러: 91(추출) / 92(OCR) / 93(청킹) / 94(청킹·DB) / 95(임베딩) / 96(임베딩·벡터DB) / 99(기타)
```

단계 완료마다 `tb_document_status`(읽기용) + `tb_document_status_log`(append-only)에 CQRS로 기록 → 장애 발생 시 마지막 성공 상태부터 재처리. 체인은 `extract → ocr → chunk → embed`, 브로커는 RabbitMQ.

### 서빙 — Adaptive RAG

`POST /answer` 한 번에 **라우팅 → CRAG → 프롬프트 분기 → 생성 → Self-RAG → Critic**이 순차 적용된다. 분류·검증·failure type 판정은 전부 **결정론적(정규식·집합 비교) → 0ms**, LLM 호출은 4곳(비교 분해 fallback · CRAG 쿼리 재작성 · 답변 생성 · 조건부 regenerate)뿐. 단계별 실행 주체·구현 위치는 [pipeline.md](docs/pipeline.md), 5-레이어 구현 상태는 [아래 섹션](#5-레이어-구현-상태) 참조.

## Evaluation snapshot

평가셋 24문항(보험 약관 0011·0012·0013), 동일 배치로 측정한 RAGAS Triad + 운영 trace 27건 (`scripts/eval_ragas.py` + `scripts/trace_summary.py` 기준):

| 지표 | 값 | SLA 목표 | 판정 |
|---|---|---|---|
| RAGAS Faithfulness (LLM judge) | **0.69** | — | judge=GPT-4o-mini (serving=Qwen3 분리, self-preference bias 회피) |
| RAGAS Answer Relevancy | **0.62** | — | |
| RAGAS Context Utilization | **0.92** | — | |
| Groundedness (regex verifier) | **mean 0.59 / p50 0.67 / p95 1.00** (n=25) | — | `supported / verifiable` — verifiable=0인 절차형 답변은 분모 제외 (RAGAS faithfulness 정의와 일치). LLM judge(0.69)보다 낮은 게 정상 — regex가 literal ref 일치를 요구해서 더 엄격 |
| Routing accuracy | **83.3%** (20/24) | — | 5-type regex classifier · 4건 mismatch는 절차/해석 경계 케이스 |
| `/answer` p50 latency | **5.6s** | ≤ 10s | ✅ |
| `/answer` p95 latency | **14.4s** | ≤ 10s | ⚠️ vLLM `gpu_memory_utilization=0.30` + KV fp8 절충 결과 |
| CRAG 트리거율 | **7.7%** (2/26) | ≤ 30% | ✅ |
| CRAG 재시도 후 score 개선률 | **100%** (2/2) | ≥ 70% | ✅ avg Δscore +0.40 |
| Critic 발동률 | **26.9%** (7/26) | — | failure_type 전체 generation_error |
| Critic regenerate improved rate | **14.3%** (1/7) | ≥ 40% | ⚠️ 작은 샘플 + regex 한계 — 한국어 다층 조항 표기("특별약관 제5장 제3조" 등)를 verifier가 흡수 못 해 hint가 무용. NLI judge 도입 트리거 |
| 서빙 trace write 실패 | 0건 | 0% | ✅ |

수치는 `data/eval/ragas_eval_result.json` / `data/eval/trace_summary_YYYYMMDD.json` 에 보관 (운영 환경에서 재측정 가능). SLA 미달 항목 후속 개선 방향은 *SLA 타겟* 섹션 참조.

**정합성 확인**: regex verifier(0.59) < LLM judge(0.69) — 같은 faithfulness 개념이지만 측정 방식 차이(literal ref 일치 vs 의미 판정) 반영. 둘 다 1.0 미만인 게 일치 = 답변 자체에 개선 여지 + verifier 정밀화 여지 양쪽 다 존재함을 정직하게 노출.

## 검증되지 않은 영역 (의도적 미구현)

본 시스템은 **결정론적 구조 검증**(정규식 기반 조항·수치 추출 + 단위 정규화 + 집합 비교)으로 1차 게이트를 구성. 다음 케이스는 본질적 한계로 **현재 못 잡음** — 검증된 한국어 보험 도메인 NLI/LLM judge가 없는 상태에서 무리하게 도입하면 false positive로 시스템 신뢰도가 오히려 하락하기 때문 (검증 안 된 컴포넌트 추가 = 신뢰도 깎기).

| 케이스 | 예시 | 현재 동작 |
|---|---|---|
| 의미 반전 | context "보장하지 아니합니다" / answer "보장됩니다" | risk=pass로 통과 (구조적 참조·수치 모두 일치) |
| 동일 조항 다른 적용 대상 | context "자가용 1,000만원, 영업용 500만원" → 자가용 질문에 "500만원" 답변 | numeric 일치라 통과 |
| 조건부 진술의 조건 누락 | context "단, 음주운전 시 제외" / answer는 "보장합니다"만 | 부분 진실로 통과 |
| 시제·양상 차이 | context "지급할 수 있습니다"(재량) / answer "지급합니다"(의무) | 단어 차이만 검출 안 됨 |
| 비표준 PII 표기 | "주민번호 9012341234567"의 hyphen·공백 변형 일부 | 정규식 missed → trace 노출 위험 |
| 미등록 단위 | "최대 1.5배 보장", "체중 80kg" | extract_numeric_facts에서 추출 0 |

**도입 조건** — `semantic_judge` slot ([src/v1/rag/grader.py](src/v1/rag/grader.py))에 NLI/LLM judge 어댑터를 주입하려면:
1. 한국어 보험 도메인 평가셋(미실패 + 의미 반전 케이스 각 50건+) 확보
2. 후보 모델(HHEM-2.1 / Azure Groundedness / multilingual NLI / GPT-4o judge) precision/recall 측정
3. **precision ≥ 0.9** 인 후보가 나오면 그때 채택 — false positive가 hard_fail 비율을 폭증시키지 않을 임계치
4. 채택 후 sidecar로 운영 trace에서 일정 기간 비교 측정 → 메인 경로 도입

이 조건이 충족되기 전까지 slot은 비워둔 상태가 가장 안전.

## 5-레이어 구현 상태

Gao et al. (2023/2024), Singh et al. (2025) taxonomy 기준:

| 레이어 | 구현 |
|---|---|
| **Naive** | retrieve → generate 기본 플로우 |
| **Advanced** | Hybrid Search (BGE-M3 Dense + Qdrant BM25 + RRF), CrossEncoder 리랭킹, Sibling ±2 복원, 토큰 예산 knapsack |
| **Modular** | `rag/router.py`·`grader.py`·`prompts.py`·`trace.py` 모듈 분리 |
| **Adaptive** | 5-type regex classifier + Dense/BM25 factor 분기 + COMPARISON decomposition (rule→llm fallback) + 5종 프롬프트 템플릿 |
| **Agentic (Reflection + Evaluator-optimizer)** | (1) CRAG retrieval gate, (2) Self-RAG 구조 검증 (조항·숫자), (3) **Critic-guided regeneration** — failure type 5분류(`generation_error`/`retrieval_gap`/`unit_error`/`semantic_mismatch`/`minor`) + hint-guided regenerate 1회 (retrieval_gap은 regenerate 금지 + escalation flag — Huang et al. ICLR 2024 외부 피드백 조건 준수). (4) **Feedback loop** — `POST /feedback` 엔드포인트 + `trace_id` 조인 기반 주간 집계 |

의미 일치 검증(NLI/HHEM)은 `semantic_judge` 주입 슬롯으로 확장 준비만 함 (기본 비활성). 의도적 제외: Planning / Tool Use / Multi-agent (closed corpus · single-shot 질의 비중 높음 · 외부 시스템 연동 없음).

## 실행

```bash
docker compose build && docker compose up -d     # 전체 빌드 + 기동
docker compose logs -f api celery                # 로그 확인
docker compose ps                                # 서비스 상태
```

상세 구성·포트·GPU 배치: [docs/architecture.md](docs/architecture.md)

## 엔드포인트

| Method | Path | 역할 |
|---|---|---|
| POST | `/api/v1/docs-rag/documents` | PDF 등록 + Celery 체인 발행 |
| GET | `/api/v1/docs-rag/documents/{service}/{id}` | 처리 상태 조회 |
| POST | `/api/v1/docs-rag/retrieve` | 하이브리드 검색 + 리랭킹 (응답에 `trace_id` 포함) |
| POST | `/api/v1/docs-rag/answer` | RAG 질의응답 (CRAG + Self-RAG + Critic-guided regeneration) |
| POST | `/api/v1/docs-rag/embeddings` | 텍스트 → BGE-M3 벡터 |
| POST | `/api/v1/docs-rag/feedback` | 사용자 피드백 수집 (trace_id + signal + free_text) |

스키마·에러 코드·필터링: [docs/api.md](docs/api.md)

## 관측 (Critic + Feedback + Input Guard 통합)

모든 `/retrieve`·`/answer` 요청은 `data/eval/trace/YYYYMMDD/traces.jsonl`에 쿼리별 1줄로 기록 (FastAPI BackgroundTasks 비동기). 핵심 의사결정 축 10개 (전체 trace는 12-섹션 — `trace_summary.py` 참조):

| 지표 | 필드 | 의사결정 축 |
|---|---|---|
| Route 분포 | `route.strategy/query_type` | Adaptive 커버리지 |
| Decomposition method | `decomposition.method` | Rule vs LLM 경로 효율 |
| Rerank score 분포 | `retrieval.rerank_scores` | CRAG threshold 타당성 |
| CRAG 전/후 score | `crag.score_before/after` | CRAG 실질 기여 |
| Risk level 분포 | `verification.risk_level` | Self-RAG 환각 검출률 |
| Claim-근거 매핑 coverage | `supported_claims_count / verifiable_claims_count` | **검증 가능한 claim**(조항/숫자 추출된)만 분모. 평문 claim은 구조적으로 매칭 불가 → 분모 제외 (RAGAS faithfulness 정의) |
| **Critic dispatch** | `critic.invoked / failure_type / action_taken / regenerate_improved` | **설계한 critic 분기가 실제로 발동·개선하는가** |
| **Feedback signal** | `tb_query_feedback.signal` + trace join | 사용자 신호와 검색 품질·risk 상관 |
| **Input Guard PII** | `input_guard.pii_found / pii_count` | 사용자 입력의 PII 노출 빈도·유형 (DLP 도구 도입 우선순위) |
| Latency breakdown | `timing_ms.*` (span별) | 병목 실측 |

**집계**:
```bash
uv run python scripts/trace_summary.py                            # 서빙 trace 12-섹션 집계 (당일)
uv run python scripts/trace_summary.py --from 20260421 --to 20260428
uv run python scripts/trace_summary.py --feedback --days 7        # 위 + Feedback DB 7일 JOIN 섹션 추가
uv run python scripts/smoke_test.py                         # DoD 11 step 자동 검증 (critic 필드 포함)
```

결과 스냅샷은 `data/eval/trace_summary_YYYYMMDD.json`에 저장 (`--feedback` 호출 시 같은 파일에 `feedback` 키 포함).

## SLA 타겟 (관측 해석 기준)

관측 숫자가 나와도 "무엇을 통과로 볼지"가 없으면 튜닝 방향을 못 잡음. 서비스 성격 기준(**전문가 검토 툴**)으로 아래 기준선을 박는다:

| 지표 | 목표 | 근거·비고 |
|---|---|---|
| `/answer` p95 latency | **≤ 10s** | 전문가가 결과 기다릴 수 있는 상한. 대화형 UX로 확장 시 ≤ 3s로 재조정 (Google/Azure 권고) |
| `/retrieve` p95 latency | **≤ 500ms** | LLM 미포함 순수 검색 |
| Trace write 실패 영향 | **서빙 200 응답 유지** | 관측은 critical path 아님 (Majors *Observability Engineering* 2022) |
| `hard_fail` 비율 (운영 trace) | **≤ 5%** | n ≥ 100 쌓인 뒤 재판정 |
| CRAG 재시도 후 score 개선률 | **≥ 70%** | 재시도가 실제로 품질을 올리는지 판단선 |
| CRAG 재시도 트리거율 | **≤ 30%** | 초기 검색이 이보다 많이 실패하면 decomposition/retrieval 설계 재검토 |
| **Critic regenerate improved rate** | **≥ 40%** | `hard_fail → pass` 전환율. 지속적으로 낮으면 `CRITIC_DISPATCH_ENABLED=false`로 rollback |
| **Feedback trace 매칭률** | **≥ 95%** | feedback이 trace와 연결되는 비율. 낮으면 trace 유실·rotate 정책 의심 |

실측은 평가셋 규모·운영 트래픽에 따라 변동 — 도구별 측정 결과는 `data/eval/trace_summary_YYYYMMDD.json` 참조 (Feedback 섹션은 `--feedback` 호출 시 같은 파일에 포함). SLA 위반·target 미달 추세 시 후속 개선 방향:
- `/answer` p95 SLA 위반 → CRAG 재시도 횟수 / vLLM 동시성 점검
- `hard_fail` 비율 높음 → context 길이·chunking 전략 재검토
- Critic regenerate improved 낮음 → hint 프롬프트 강화 또는 NLI judge 도입
- Feedback 매칭률 낮음 → trace rotate·write 실패 점검

## 주요 기능

- **관측 인프라** — 12-섹션 trace 집계(route·decomposition·rerank·CRAG·verification(+groundedness 0~1 percentile)·provenance·latency·errors·critic·input_guard·output_guard) + DoD smoke test 자동 검증, 응답 스키마 슬림(상세는 trace로) + SLA 기준선
- **평가 도구** — RAGAS Triad (Faithfulness/Answer Relevancy/Context Utilization) + classifier routing accuracy(`expected_type` 24개 라벨로 회귀 감지) + 임베딩 공간 헬스 `eval_index_health` (Dispersion + Confusion Rate). Retrieval Recall@k/MRR은 라벨링 비용 부담으로 보류 — Context Utilization이 proxy 역할
- **Critic verifier-guided regeneration (실험적)** — `classify_failure` 5분류기(generation_error / retrieval_gap / unit_error / semantic_mismatch / minor) + `build_hint`로 외부 피드백 생성 → `REGENERATE_WITH_HINT_PROMPT` 1회 재생성. retrieval_gap·semantic_mismatch는 regenerate 금지 + escalation flag (Huang et al. ICLR 2024 자기교정 함정 회피).
  - **현재 측정**: regenerate improved rate **14.3%** (1/7, 평가셋 24문항 기준) — SLA target 40% 미달. 한국어 다층 조항 표기("특별약관 제5장 제3조") · "보통약관 제43조" 같은 패턴을 정규식 verifier가 collapse해서 hint가 무용한 케이스 다수. 도입 가치를 적은 트래픽에서 검증 중. `CRITIC_DISPATCH_ENABLED=false` 한 줄로 즉시 비활성화 가능. **hard_fail → pass 전환은 일부 발생하지만 통계적으로 유의미한 개선 미확인 상태** — 이걸 인지하고 운영
  - **검증된 부분**: failure_type 분류는 정규식 기반이라 결정론적으로 작동 (test_grader.py Critic dispatch 섹션 28+ 케이스)
  - **검증 안 된 부분**: regenerate 후 답변 품질 개선 정도 — RAGAS·실 사용자 피드백 누적 후 재판정
- **Feedback endpoint** — `POST /feedback` + `tb_query_feedback` + `FeedbackRepository` + `trace_id` 응답 노출 + `trace_summary.py --feedback` 집계. **현재 실 사용자 UI 미연동 → 진짜 사용자 신호 0건**. 엔드포인트는 미래 UI 통합을 위한 API 계약 + synthetic proxy의 receiver로만 사용 중. 집계·매칭률·trace 조인 파이프라인 동작 검증이 주 목적
- **Synthetic feedback proxy (파이프라인 검증용)** — `eval_ragas.py --submit-feedback`이 RAGAS Faithfulness를 signal로 자동 매핑(≥0.7→up / ≥0.4→reformulated / <0.4→down). **이 매핑 자체는 검증 안 됨** — RAGAS 점수와 실 사용자 만족도의 상관관계는 가정일 뿐. 실 UI 통합 시 이 proxy 레이어 즉시 제거 (free_text의 `"synthetic from RAGAS"` 라벨로 실 데이터와 구분)
- **Guardrails 6계층 (4계층 구현 + 2계층 의도적 미구현)**

  | 계층 | 구현 상태 |
  |---|---|
  | Input Guard | ✅ PII 마스킹 5종(RRN/CARD/PHONE/ACCOUNT/EMAIL) + Prompt Injection 정규식 7종 + zero-width 차단 (`guards/pii.py`, `guards/injection.py`). OWASP LLM06 + LLM01. Microsoft Presidio · Lakera Guard 어댑터로 교체 가능 |
  | Retrieval Guard | ✅ CRAG retrieval evaluator — top-1 rerank score < 0.3 시 vLLM 쿼리 재작성 → 재검색 (최대 2회). 검색 게이트 |
  | Grounding Guard | ✅ verify_answer (조항·별표·숫자 정규식 검증) + Groundedness Score 0~1 (RAGAS faithfulness 패턴) + inline citation (claim ↔ chunk 매핑, Anthropic Citations API 패턴) |
  | Output Guard | ✅ role token leak (ChatML/Llama Instruct/system: prefix) silent 제거 + 욕설 라벨 (`guards/output.py`). OWASP LLM02 |
  | Access Guard | ❌ **외부 노출 시 도입 예정** — 사내 단일 vLLM 환경에서 자연 큐잉되어 YAGNI. 트리거: 외부 클라이언트 노출 시 slowapi 30분 도입 가능 |
  | Action Guard | ❌ **read-only RAG라 자리 없음** — Tool calling / DB write / 외부 API 호출 0건이라 정당화 안 됨. 트리거: tool calling 도입 시 |
- **RAGAS Judge LLM 분리** — `eval_ragas.py` judge를 vLLM(Qwen3) → OpenAI GPT-4o-mini로 분리해 self-preference bias 회피 (Zheng et al. NeurIPS 2023 MT-Bench). `OPENAI_API_KEY` 미설정 시 vLLM fallback + 결과 JSON에 `"biased"` 라벨로 audit
- **Feature flag 2종** — `FEEDBACK_ENABLED` / `CRITIC_DISPATCH_ENABLED` env 한 줄로 즉시 비활성화 (코드 변경·재배포 불필요)

## 의도적 미구현 (Anti-features)

이 코드베이스에 **의도적으로 들어있지 않은 것**들과 사유. 향후 PR/이슈에서 도입 제안 시 이 섹션 먼저 확인 권장 — 도입 트리거 조건이 충족됐는지부터 점검해야 dead infrastructure 추가 회피.

| 항목 | 미구현 사유 | 도입 트리거 |
|---|---|---|
| Rate Limit (slowapi) | 사내 단일 vLLM 환경에서 자연 큐잉되어 무용 | 외부 클라이언트 노출 / 다중 vLLM 워커 도입 |
| Action Guard | read-only RAG라 자리 없음 (tool calling 0건) | LLM이 외부 시스템에 부작용 가능한 동작 추가 |
| API Key 인증 | 운영 정책 미정 단계 | multi-tenant / 외부 클라이언트 발급 |
| Per-document 권한 | 요구사항 없음 (모든 사용자가 모든 문서 접근) | 권한 분리 요구 |
| Structured Output (vLLM `guided_json`) | Qwen3 한국어 guided generation 안정성 미검증 — 답변 품질 저하 위험 | 평가셋에서 답변 형식 일관성 ↓ 측정 시 |
| CI Gate (RAGAS 회귀 자동 차단) | 운영 트래픽 안정 + 평가셋 50건+ 시점 | 평가셋 규모 충분 |
| Human Eval (도메인 전문가 답변 평가) | 평가셋 24건 규모로 통계 의미 약함 | 평가셋 50건+ + 전문가 시간 확보 |
| Retrieval 평가 (Recall@k / MRR) | golden chunk_id 라벨링 비용 부담 (도메인 전문가 3~6시간) | 임베딩 모델 비교·rerank threshold A/B 시. 현재는 Context Utilization이 proxy |
| Multi-Domain RAG | 단일 도메인 (보험 약관) | 2개 이상 도메인 추가 시 rule/embedding/LLM 3단계 라우터 |
| Realtime Feature Pipeline (Feast/Kafka) | 사용자 행동이 검색 결과에 영향 X (stateless RAG) | 개인화 / 클릭 기반 rerank weight / feedback 즉시 반영 |
| LangGraph StateGraph | 단일턴 read-only RAG에 cycle 깊이 max 2 — imperative if/while + `trace_span`이 더 단순 | 멀티턴 / multi-tool / human-in-the-loop / cyclic agent 도입 |
| NER 기반 PII (Presidio) | 한국어 보험 도메인은 정규식 5종으로 99% 커버 | 비정형 PII (이름·주소·기관명) 빈도 ↑ 측정 시 |
| Semantic mismatch judge (NLI/HHEM) | 한국어 보험 도메인 precision ≥ 0.9 검증된 모델 부재 — false positive 폭증 위험 | 평가셋 1000+에서 후보 NLI 모델 측정 후 채택 |

→ 이 표 자체가 "문서에 없는 것을 추가 제안하기 전 sanity check" 역할. 도입 트리거 조건이 안 맞은 상태에서 덧붙이면 dead infrastructure로 운영 복잡도만 증가.

## 확장 지점 (조건부 도입)

- **Semantic mismatch judge** — NLI classifier / HHEM-2.1 / LLM judge로 `semantic_judge` 슬롯 채우기 (의미 반전 감지)
- **Multi-domain 라우팅** — 도메인 추가 시 rule/embedding/LLM 3단계 라우터 + 도메인별 config
- **Eval Gold Set Store** — `tb_eval_sample` + Baseline tracker로 RAGAS Triad 회귀 자동 감지
- **Multi-aspect decomposition** — N-way 비교·다축 질의를 위한 Planner 도입
- **실시간 피처 파이프라인** — 대규모 사용자 시점 Redis Streams → Kafka 도입
- **LangGraph 기반 v2 에이전트 플로우** — 다음 트리거 중 하나라도 들어오면 별도 `/v2/...` 엔드포인트로 신설:
  - 멀티턴 conversation (follow-up이 이전 턴 state·답변에 따라 분기)
  - Multi-tool agent (보험료 계산 API + 정책 검색 + 티켓 생성 등 여러 툴 condition별 호출)
  - Human-in-the-loop (담당자 승인 노드, interrupt/resume)
  - Cyclic agent (plan → act → observe → re-plan 다회 반복)

  현재 단일턴 read-only RAG 범위에선 imperative if/while + `trace_span` context manager가 graph state machine보다 가독성·디버깅·observability(`rec.critic`/`rec.verification` 같은 비즈니스 메타데이터는 graph로 가도 동일하게 명시 작성 필요) 모두 우월. v1 갈아엎지 않고 v2 신설로 전환 경로 분리 — *"언제 LangGraph가 맞고 언제 아닌지 안다"* 가 senior 시그널.

## 모델 고도화 로드맵 (측정 → 조건부 파인튜닝)

파인튜닝(대조학습·LoRA)은 비싸고 되돌리기 어렵다 — **측정된 이득만 메인 경로에** 원칙을 학습에도 적용한다. 순서가 아니라 **트리거 게이트**: 먼저 측정 기반을 세우고, 그 결과가 어느 축을 파인튜닝할지 가른다. 전체 설계·데이터·근거는 [docs/roadmap.md](docs/roadmap.md).

| Phase | 내용 | 트리거 | 데이터 |
|---|---|---|---|
| **0. 측정 기반** (선행 필수) | RAGAS에 Context Precision/Recall 추가 + Retrieval Recall@k/MRR 도입 + before/after A/B 하네스 | — (게이트 자체) | reverse-QA 코퍼스 마이닝 → silver golden |
| **1. BGE-M3 대조학습** | InfoNCE 임베딩 파인튜닝 (리랭커 먼저 → 임베더). 1024차원 유지·신규 컬렉션 A/B | Phase 0가 **retrieval-bound** 판정 (Recall@k·도메인 용어 취약) | 구조·참조 positive + BM25/Dense hard negative + **critic 인접-조항 오류를 hard negative로** |
| **2. Qwen3 LoRA** | 도메인 어댑터 SFT (근거·형식 준수). vLLM LoRA hot-load + `LLM_ADAPTER` 플래그 | Phase 0가 **generation-bound** 판정 (근거는 있는데 Faithfulness 낮음) | 고품질 답변 rejection sampling (self-distillation, echo-chamber 완화) |

**공통 채택 게이트**: held-out A/B 유의미 개선 + 일반 쿼리 회귀 없음 + 즉시 롤백 가능 — 셋 다 충족 전까지 sidecar/실험 경로. 현재 trace/critic 데이터가 병목 진단이자 hard-negative 소스가 되어 측정-먼저 흐름과 맞물린다.

## 평가·벤치 도구

> 자주 쓰는 명령은 [Makefile](Makefile) 참조. 아래는 각 스크립트의 옵션·동작 상세.

```bash
# 관측 (운영 집계는 trace_summary.py 단일 진입점 — --feedback 플래그로 DB JOIN 섹션 포함)
uv run python scripts/trace_summary.py                  # 서빙 trace 12-섹션 집계 (critic dispatch 포함)
uv run python scripts/trace_summary.py --feedback       # 위 + Feedback DB 7일 JOIN 섹션 추가
uv run python scripts/smoke_test.py               # DoD 11 step 자동 검증

# 평가 (Judge LLM = OpenAI GPT-4o-mini, Serving = vLLM/Qwen3 — self-preference bias 회피)
OPENAI_API_KEY=sk-... uv run python scripts/eval_ragas.py        # RAGAS Triad (정상 모드)
uv run python scripts/eval_ragas.py                              # 키 없으면 vLLM judge로 fallback (점수에 "biased" 라벨)
uv run python scripts/eval_ragas.py --basic                      # 답변만 수집 (judge 스킵 — trace 축적용)
OPENAI_API_KEY=sk-... uv run python scripts/eval_ragas.py --submit-feedback   # RAGAS → synthetic feedback signal 매핑·제출
uv run python scripts/eval_ocr.py                                # OCR 품질
uv run python scripts/eval_index_health.py                       # Qdrant 벡터 공간 헬스 (dispersion + confusion)

# 단위 테스트 (integration 마크는 자동 skip — host에서 모델 의존성 회피)
uv run pytest tests/ -v                                          # rag + guards 단위 (host)
docker compose exec api uv run pytest tests/ -v -m integration   # E2E (docker 안에서만)
```

## 기술 스택

- **Runtime**: Python 3.10, FastAPI, uv, Celery + RabbitMQ, Docker Compose
- **Embedding**: BGE-M3 1024차원 + Qdrant BM25, RRF 융합, INT8 양자화
- **Reranking**: BGE-reranker-v2-m3 (CrossEncoder)
- **LLM**: Qwen3-14B-AWQ (vLLM, TP=1, util 0.30, KV fp8)
- **OCR**: PaddleOCR PP-StructureV3 (layout + table + formula + OCR, 현재 CPU 고정)
- **Storage**: PostgreSQL (메타) + Qdrant (벡터DB)
- **Hardware**: Ubuntu 24.04, RTX PRO 6000 Blackwell ×4 (96GB each, GPU 0 통합 사용)

## 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 시스템 구성, 서비스 포트·GPU 배치, 데이터 흐름, 성능 특성, 장애 대응 |
| [docs/api.md](docs/api.md) | REST 엔드포인트 상세 (요청·응답 스키마, 에러 코드, `/feedback` + synthetic proxy 포함) |
| [docs/pipeline.md](docs/pipeline.md) | RAG 서빙 파이프라인 (쿼리 라우팅, CRAG, 프롬프트 분기, Self-RAG 검증, **Critic failure type 분기**) |
| [docs/chunking.md](docs/chunking.md) | 청킹 전략 (adaptive/fixed, OCR 청크, sibling 복원) |
| [docs/roadmap.md](docs/roadmap.md) | 모델 고도화 로드맵 (측정 기반 → 조건부 대조학습·LoRA, 트리거 조건) |
| [CLAUDE.md](CLAUDE.md) | AI 에이전트 작업 지침 (명령어, 연쇄 수정, 도메인 용어) |
