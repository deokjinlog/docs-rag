<div align="center">

# docs-rag

**검증까지 내장한 한국어 문서 RAG 파이프라인**

*Production-grade Korean RAG for structured PDFs — 하이브리드 검색 · 자기 교정 · 정직한 평가*

<p>
  <img src="https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white" alt="Python 3.10">
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Celery-RabbitMQ-37814A?logo=celery&logoColor=white" alt="Celery">
  <img src="https://img.shields.io/badge/vLLM-Qwen3--14B--AWQ-1a56db" alt="vLLM">
  <img src="https://img.shields.io/badge/Qdrant-Dense%2BBM25-DC244C?logo=qdrant&logoColor=white" alt="Qdrant">
  <img src="https://img.shields.io/badge/eval-RAGAS-6c47ff" alt="RAGAS">
  <img src="https://img.shields.io/badge/License-MIT-3da639" alt="MIT License">
</p>

[아키텍처](docs/architecture.md) · [API](docs/api.md) · [파이프라인](docs/pipeline.md) · [청킹](docs/chunking.md) · [로드맵](docs/roadmap.md) · [Notion 설계 근거](https://www.notion.so/DocsRAG-31b9fb2de50b80b59e04d05d8985ceca)

</div>

---

약관·법령·매뉴얼 같은 **한국어 구조화 PDF**를 등록하면 — 자동으로 추출·OCR·청킹·임베딩해서 인덱싱하고, **하이브리드 검색 + LLM 답변 생성 + 자기 검증(Self-RAG)·자기 교정(CRAG·Critic)** 까지 처리하는 End-to-End 파이프라인. 운영 corpus는 보험·법령이지만 **도메인 비종속** — 라우팅 정규식/프롬프트만 바꾸면 재사용된다.

> 차별점은 *"동작하는 RAG"* 가 아니라 **"틀렸을 때 스스로 알고, 무엇을 못 하는지 정직하게 문서화한 RAG"** 다. 평가 지표 · 아직 못 잡는 케이스 · 의도적 미구현과 도입 트리거까지 공개한다 ([설계 철학과 한계](#설계-철학과-한계)).

```mermaid
flowchart LR
    PDF[PDF] --> ING["수집: extract · ocr · chunk · embed"] --> QD[("Qdrant<br/>Dense + BM25")]
    Q[질의] --> RET["하이브리드 검색 + Rerank"] --> GEN["LLM 생성"] --> VER["구조 검증<br/>hard_fail 플래그"] --> ANS[답변]
    QD -.검색.-> RET
```

> 시스템 구성도는 [docs/architecture.md](docs/architecture.md), 서빙 분기(CRAG·Critic는 opt-in)는 [docs/pipeline.md](docs/pipeline.md).

## 핵심 특징

방향은 **단순하지만 견고하게** — 품질의 핵심(견고한 인제스천 · 검색 · 검증)에 집중하고, 복잡한 레이어는 만들되 측정이 요구할 때만 켠다.

- **구조 보존 인제스천** — ODL로 다단 레이아웃·읽기순서 보존, PaddleOCR PP-StructureV3로 스캔·이미지 **표를 HTML 구조 복원 → 마크다운 그리드**. 상태코드 기반 재처리로 실패 지점부터 복구 (가장 어렵고 견고해야 할 부분)
- **하이브리드 검색 + Rerank** — BGE-M3 Dense + Qdrant BM25 RRF 융합 + CrossEncoder 리랭킹 + sibling ±2 복원. **품질의 핵심** — 실측 rerank top-1 mean 0.85
- **공짜 구조 검증** — Self-RAG 정규식 검증(0ms)으로 조항·수치 대조 → hard_fail 플래그. 전문가 검토 툴이라 자동 교정 대신 **근거와 함께 플래그**
- **평가 기반 자생 개선** — RAGAS + retrieval 지표로 측정 → 컨텍스트 품질 또는 파인튜닝으로 반영하는 루프 (judge 분리로 self-preference 회피)
- **측정으로 걸러낸 복잡도** — Adaptive 라우팅 · CRAG · Critic · 12-섹션 trace · 4층 가드레일은 **만들어 두되 트래픽·측정이 요구할 때만 켜는 opt-in**. 무엇이 왜 필요/불필요했는지 [설계 회고](docs/design-retrospective.md)에 실측 공개

## 빠른 시작

```bash
# 1. 전체 스택 빌드 + 기동 (API · Celery · vLLM · Qdrant · PostgreSQL · RabbitMQ · OCR)
docker compose build && docker compose up -d
docker compose ps

# 2. 문서 등록 → 비동기 extract→ocr→chunk→embed 체인 발행
curl -X POST localhost:8002/api/v1/docs-rag/documents \
  -H 'Content-Type: application/json' \
  -d '{"service_code":"01","document_id":"0001","document_name":"약관.pdf","document_path":"/data/input/약관.pdf"}'

# 3. 질의 → CRAG + Self-RAG + Critic, 응답에 trace_id·citations 포함
curl -X POST localhost:8002/api/v1/docs-rag/answer \
  -H 'Content-Type: application/json' \
  -d '{"query":"무면허운전 시 보험금 지급이 되나요?","service_code":"01"}'
```

구성·포트·GPU 배치·장애 대응은 [docs/architecture.md](docs/architecture.md), 명령 alias는 [Makefile](Makefile).

## 어떻게 동작하나

### 수집 — `extract → ocr → chunk → embed`

| 스테이지 | 하는 일 | 핵심 설계 |
|---|---|---|
| **extract** | ODL로 PDF → Markdown + 내부 이미지 | ≤200p `docling-fast`(hybrid ML) → 품질 미달 시 Java fallback / >200p Java-direct. 읽기순서·구조 보존, 헤더·푸터·워터마크 제거, 로컬 실행·데이터 유출 0% |
| **ocr** | 삽입·스캔 이미지를 PaddleOCR로 구조화 | PP-StructureV3(layout+table+formula+OCR). **표는 HTML 구조로 복원 → 마크다운 그리드 변환**, 6단계 입구 필터로 garbage 컷 (아래 상세) |
| **chunk** | 정규화 + 룰베이스 청킹 + OCR 청크 합류 | 경계 3원칙 — heading→새 청크 / paragraph·table·list 의미 단위 보존 / 조항 번호→참조 관계 기록. Adaptive(헤딩 트리·sibling) / Fixed(800·150) |
| **embed** | 청크 → BGE-M3 → Qdrant 적재 | 1024d·Cosine·INT8. Dense와 BM25 벡터(`content-bm25`) 병행, 1000개 배치 upsert. 리랭커 `bge-reranker-v2-m3` |

<details>
<summary><b>PaddleOCR — 표·이미지 구조 복원 (표 → HTML → 마크다운)</b></summary>

스캔본·이미지형 페이지, 텍스트 PDF에 박힌 이미지를 개별 이미지 단위로 PP-StructureV3에 태워 구조를 복원한다.

- **SR (Super-Resolution)** — 저화질 스캔을 300+DPI로 보정해 인식률 향상
- **표 구조 재구성** — 셀 병합·헤더 행 인식 후 PP-StructureV3의 `table_res_list` **HTML**을 celery `_html_table_to_markdown()`이 **마크다운 표**로 변환. 평문에 안 섞이게 `chunk_type="table"` 별도 청크로 저장 → LLM이 표를 표로 인식
- **`is_valid_image` 6단계 입구 필터** — 파일크기 / 최소차원 / figure 최소크기 / 종횡비 / 최대차원 / 단색(stddev) 로 1px spacer·아이콘·로고·QR·가로띠·투명 마스크 컷 → garbage에 paddle 호출 안 함
- **저장 산출물** — 이미지 옆 `_ocr.json`(rec_texts·layout_boxes·parsing_blocks) + `_ocr_layout.png`, confidence 컷(0.5) 통과분만 기록
- **CPU 고정** — Blackwell sm_120이 현재 paddle 빌드에 미포함. Paddle 3.4+ 지원 시 토글로 GPU 복귀

같은 이미지의 image/table 청크는 동일 `heading_path`를 공유 → sibling 복원 시 함께 context로. 상세: [docs/chunking.md](docs/chunking.md)

</details>

### 서빙 — `POST /answer`

한 번의 호출에 **라우팅 → CRAG → 프롬프트 분기 → 생성 → Self-RAG → Critic**이 순차 적용된다. 분류·검증·failure type 판정은 전부 결정론적(정규식·집합 비교)이라 0ms, LLM 호출은 4곳(비교 분해 fallback · CRAG 재작성 · 답변 생성 · 조건부 regenerate)뿐. 단계별 실행 주체는 [docs/pipeline.md](docs/pipeline.md).

<details>
<summary><b>상태 코드 — 실패 지점부터 재처리</b></summary>

```
00(대기) → 22(추출중) → 21(추출완료) → 24(OCR중) → 23(OCR완료)
→ 32(청킹중) → 31(청킹완료) → 42(임베딩중) → 43(임베딩완료) → 41(벡터DB적재) → 11(완료)
에러: 91(추출) / 92(OCR) / 93(청킹) / 94(청킹·DB) / 95(임베딩) / 96(임베딩·벡터DB) / 99(기타)
```

단계 완료마다 `tb_document_status`(읽기용) + `tb_document_status_log`(append-only)에 CQRS로 기록 → 장애 시 마지막 성공 상태부터 재처리. 브로커는 RabbitMQ.

</details>

## API 엔드포인트

| Method | Path | 역할 |
|---|---|---|
| `POST` | `/api/v1/docs-rag/documents` | PDF 등록 + Celery 체인 발행 |
| `GET` | `/api/v1/docs-rag/documents/{service}/{id}` | 처리 상태 조회 |
| `POST` | `/api/v1/docs-rag/retrieve` | 하이브리드 검색 + 리랭킹 |
| `POST` | `/api/v1/docs-rag/answer` | RAG 질의응답 (CRAG + Self-RAG + Critic) |
| `POST` | `/api/v1/docs-rag/embeddings` | 텍스트 → BGE-M3 벡터 |
| `POST` | `/api/v1/docs-rag/feedback` | 피드백 수집 (`trace_id` + signal) |

스키마·에러 코드·필터링: [docs/api.md](docs/api.md)

## 평가

평가셋 24문항, RAGAS Triad + 운영 trace 27건. judge는 GPT-4o-mini로 **분리**(serving=Qwen3)해 self-preference bias 회피.

| 지표 | 값 | 목표 | 판정 |
|---|---|---|---|
| RAGAS Faithfulness | **0.69** | — | LLM judge=GPT-4o-mini |
| RAGAS Answer Relevancy | **0.62** | — | |
| RAGAS Context Utilization | **0.92** | — | |
| Routing accuracy | **83.3%** (20/24) | — | 5-type regex classifier |
| `/answer` p50 / p95 | **5.6s / 14.4s** | ≤ 10s | p95 ⚠️ (vLLM util 0.30 + KV fp8 절충) |
| CRAG 트리거율 / 재시도 개선 | **7.7% / 100%** | ≤30% / ≥70% | ✅ |
| Critic regenerate improved | **14.3%** (1/7) | ≥ 40% | ⚠️ 소표본 + regex 한계 |
| 서빙 trace write 실패 | **0건** | 0% | ✅ |

<details>
<summary>수치 해석 · 정합성 노트</summary>

- 수치는 `data/eval/ragas_eval_result.json` / `data/eval/trace_summary_YYYYMMDD.json`에 보관.
- Groundedness(regex verifier) mean 0.59 < LLM judge 0.69 — 같은 개념이지만 literal ref 일치를 요구해 더 엄격. 둘 다 1.0 미만 = 답변·verifier 양쪽 개선 여지를 정직하게 노출.
- Critic regenerate가 낮은 이유: 한국어 다층 조항 표기("특별약관 제5장 제3조")를 정규식 verifier가 collapse해 hint가 무용한 케이스 다수. `CRITIC_DISPATCH_ENABLED=false`로 즉시 비활성화 가능.

</details>

## 설계 철학과 한계

> **검증된 것(측정된 이득)만 메인 경로에.** 검증 안 된 컴포넌트를 끼우면 false positive가 신뢰도를 오히려 깎는다. 그래서 무엇을 왜 안 만들었고, 무엇을 아직 못 잡는지, 언제 도입할지를 전부 문서로 남긴다.

<details>
<summary><b>5-레이어 구현 상태</b> (Naive → Advanced → Modular → Adaptive → Agentic)</summary>

Gao et al. (2023/2024), Singh et al. (2025) taxonomy 기준:

| 레이어 | 구현 |
|---|---|
| **Naive** | retrieve → generate 기본 플로우 |
| **Advanced** | Hybrid Search (BGE-M3 Dense + Qdrant BM25 + RRF), CrossEncoder 리랭킹, Sibling ±2 복원, 토큰 예산 knapsack |
| **Modular** | `rag/router.py`·`grader.py`·`prompts.py`·`trace.py` 모듈 분리 |
| **Adaptive** | 5-type regex classifier + Dense/BM25 factor 분기 + COMPARISON decomposition (rule→llm fallback) + 5종 프롬프트 |
| **Agentic** | CRAG retrieval gate + Self-RAG 구조 검증 + Critic-guided regeneration(실패 5분류, retrieval_gap은 regenerate 금지 + escalation — Huang et al. ICLR 2024) + Feedback loop(`POST /feedback` + `trace_id` 조인) |

의미 일치 검증(NLI/HHEM)은 `semantic_judge` 슬롯으로 준비만(기본 비활성). 의도적 제외: Planning / Tool Use / Multi-agent.

</details>

<details>
<summary><b>검증되지 않은 영역</b> (구조 검증이 못 잡는 케이스)</summary>

결정론적 구조 검증(정규식 조항·수치 추출 + 단위 정규화 + 집합 비교)으로 1차 게이트를 구성. 아래는 본질적 한계로 **현재 못 잡음** — 검증된 한국어 도메인 judge 없이 무리 도입하면 false positive로 신뢰도가 하락.

| 케이스 | 예시 | 현재 동작 |
|---|---|---|
| 의미 반전 | "보장하지 아니합니다" → "보장됩니다" | pass 통과 |
| 동일 조항 다른 대상 | "자가용 1,000만/영업용 500만" → 자가용에 "500만" | numeric 일치라 통과 |
| 조건부 진술 조건 누락 | "단, 음주운전 시 제외" → "보장"만 | 부분 진실로 통과 |
| 시제·양상 차이 | "지급할 수 있습니다"(재량) → "지급합니다"(의무) | 검출 안 됨 |
| 미등록 단위 | "최대 1.5배 보장", "체중 80kg" | 추출 0 |

**도입 조건** (`semantic_judge`에 NLI/LLM judge 주입 시): 도메인 평가셋(정상+반전 각 50건+) → 후보 precision/recall 측정 → **precision ≥ 0.9** 통과분만 채택 → sidecar 비교 후 메인. 충족 전까지 비워두는 게 안전.

</details>

<details>
<summary><b>가드레일 6계층</b> (4 구현 + 2 의도적 미구현)</summary>

| 계층 | 상태 |
|---|---|
| Input Guard | ✅ PII 마스킹 5종 + Injection 정규식 7종 + zero-width 차단 (OWASP LLM06+LLM01) |
| Retrieval Guard | ✅ CRAG evaluator — top-1 rerank < 0.3 시 재작성 → 재검색(최대 2회) |
| Grounding Guard | ✅ verify_answer(조항·별표·숫자) + Groundedness 0~1 + inline citation |
| Output Guard | ✅ role token leak silent 제거 + 욕설 라벨 (OWASP LLM02) |
| Access Guard | ❌ 사내 단일 vLLM 자연 큐잉 → YAGNI. 트리거: 외부 노출 |
| Action Guard | ❌ read-only RAG라 자리 없음. 트리거: tool calling 도입 |

</details>

<details>
<summary><b>의도적 미구현 (Anti-features)</b> — 무엇을 왜 안 넣었고 언제 넣는가</summary>

도입 제안 전 이 표부터 확인 — 트리거가 안 맞은 상태에서 덧붙이면 dead infrastructure로 복잡도만 증가.

| 항목 | 미구현 사유 | 도입 트리거 |
|---|---|---|
| Rate Limit (slowapi) | 사내 단일 vLLM 자연 큐잉 | 외부 노출 / 다중 워커 |
| API Key 인증 | 운영 정책 미정 | multi-tenant / 외부 클라이언트 |
| Structured Output (`guided_json`) | Qwen3 한국어 guided 안정성 미검증 | 형식 일관성 ↓ 측정 시 |
| CI Gate (RAGAS 회귀 차단) | 평가셋 규모 부족 | 평가셋 50건+ |
| Retrieval 평가 (Recall@k/MRR) | golden chunk 라벨링 비용 | 임베딩·rerank A/B 시 |
| Multi-Domain RAG | 단일 도메인 | 2+ 도메인 시 3단계 라우터 |
| LangGraph StateGraph | 단일턴엔 if/while + `trace_span`이 더 단순 | 멀티턴 / multi-tool / HITL |
| Semantic judge (NLI/HHEM) | precision ≥ 0.9 검증 모델 부재 | 평가셋 1000+ 후보 측정 후 |

</details>

<details>
<summary><b>SLA 타겟</b> (관측 숫자 해석 기준선)</summary>

서비스 성격(전문가 검토 툴) 기준선:

| 지표 | 목표 | 근거 |
|---|---|---|
| `/answer` p95 | ≤ 10s | 전문가 대기 상한 (대화형 확장 시 ≤ 3s) |
| `/retrieve` p95 | ≤ 500ms | LLM 미포함 순수 검색 |
| `hard_fail` 비율 | ≤ 5% | n ≥ 100 뒤 재판정 |
| CRAG 개선률 / 트리거율 | ≥70% / ≤30% | 낮으면 retrieval 설계 재검토 |
| Critic regenerate improved | ≥ 40% | 낮으면 `CRITIC_DISPATCH_ENABLED=false` |
| Feedback trace 매칭률 | ≥ 95% | 낮으면 trace 유실 의심 |

</details>

## 로드맵 — 측정 → 조건부 파인튜닝

파인튜닝(대조학습·LoRA)은 비싸고 되돌리기 어렵다 — **측정된 이득만 메인 경로에** 원칙을 학습에도 적용한다. 순서가 아니라 **트리거 게이트**: 먼저 측정 기반을 세우고, 그 결과가 어느 축을 파인튜닝할지 가른다. 전체 설계는 [docs/roadmap.md](docs/roadmap.md).

| Phase | 내용 | 트리거 |
|---|---|---|
| **0. 측정 기반** (선행) | RAGAS Context Precision/Recall + Retrieval Recall@k/MRR + A/B 하네스 | — (게이트 자체) |
| **1. BGE-M3 대조학습** | InfoNCE 임베딩 파인튜닝 (리랭커 먼저), 신규 컬렉션 A/B | Phase 0 **retrieval-bound** |
| **2. Qwen3 LoRA** | 도메인 어댑터 SFT, vLLM LoRA + `LLM_ADAPTER` 플래그 | Phase 0 **generation-bound** |

데이터는 코퍼스 마이닝 전제, 채택은 held-out A/B 개선 + 회귀 없음 + 즉시 롤백 게이트 통과 후 메인 경로로.

## 관측 · 평가 도구

<details>
<summary>trace 집계 · RAGAS · OCR · 인덱스 헬스 명령</summary>

```bash
# 관측 (trace_summary.py 단일 진입점)
uv run python scripts/trace_summary.py                  # 서빙 trace 12-섹션 집계 (critic 포함)
uv run python scripts/trace_summary.py --feedback       # + Feedback DB 7일 JOIN
uv run python scripts/smoke_test.py                     # DoD 11-step 자동 검증

# 평가 (Judge=GPT-4o-mini / Serving=vLLM Qwen3 분리)
OPENAI_API_KEY=sk-... uv run python scripts/eval_ragas.py            # RAGAS Triad
uv run python scripts/eval_ocr.py                                   # OCR 품질
uv run python scripts/eval_index_health.py                         # 벡터 공간 (dispersion + confusion)

# 테스트 (integration 마크는 host에서 자동 skip)
uv run pytest tests/ -v
docker compose exec api uv run pytest tests/ -v -m integration      # E2E (docker 내부)
```

관측 축 10개(전체 12-섹션): route · decomposition · rerank score · CRAG 전후 · risk_level · claim-근거 coverage · critic dispatch · feedback signal · input guard PII · latency. 각 축이 특정 설계 결정을 검증한다.

</details>

## 기술 스택

| 영역 | 구성 |
|---|---|
| Runtime | Python 3.10 · FastAPI · uv · Celery + RabbitMQ · Docker Compose |
| 검색·임베딩 | BGE-M3 1024d + Qdrant BM25 · RRF · INT8 양자화 · `bge-reranker-v2-m3` |
| LLM | Qwen3-14B-AWQ (vLLM, TP=1, util 0.30, KV fp8) |
| OCR | PaddleOCR PP-StructureV3 (layout+table+formula+OCR, 현재 CPU) |
| 저장 | PostgreSQL(메타) + Qdrant(벡터DB) |
| 하드웨어 | Ubuntu 24.04 · RTX PRO 6000 Blackwell ×4 (96GB, GPU 0 통합) |

## 기여

이슈·PR 환영합니다.

- **개발 환경** — `docker compose up -d` 후 `uv run pytest tests/ -v` (integration 마크는 host에서 자동 skip). 설정은 `.env`로 관리하며 하드코딩 금지.
- **코딩 규약 · 연쇄 수정 지점** — [CLAUDE.md](CLAUDE.md) 참조. DB 스키마·상태 관리·Qdrant payload·Celery 체인은 여러 파일을 함께 고쳐야 하는 지점이 정리돼 있다.
- **새 기능 제안 전** — [의도적 미구현](#설계-철학과-한계) 표에서 도입 트리거가 충족됐는지 먼저 확인. 새 검증·평가 컴포넌트는 **precision ≥ 0.9 측정 후에만** 메인 경로에 (sidecar/slot 우선).
- **커밋 메시지** — `type: 요약` (docs / feat / fix / refactor). TraceRecord 필드 추가 등은 aggregator·smoke_test 동시 갱신.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 시스템 구성, 포트·GPU 배치, 데이터 흐름, 성능, 장애 대응 |
| [docs/api.md](docs/api.md) | REST 엔드포인트 상세 (스키마, 에러 코드, 멱등성) |
| [docs/pipeline.md](docs/pipeline.md) | RAG 서빙 (라우팅, CRAG, 프롬프트 분기, Self-RAG, Critic) |
| [docs/chunking.md](docs/chunking.md) | 청킹 전략 (adaptive/fixed, OCR 3단계 필터, sibling 복원) |
| [docs/roadmap.md](docs/roadmap.md) | 모델 고도화 로드맵 (측정 → 조건부 대조학습·LoRA) |
| [docs/design-retrospective.md](docs/design-retrospective.md) | 설계 회고 — 레이어별 실측 가성비, 표준 RAG 대비, 평가 기반 개선 전략 |
| [CLAUDE.md](CLAUDE.md) | AI 에이전트 작업 지침 (명령어, 연쇄 수정, 도메인 용어) |
