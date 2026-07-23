# RAG 파이프라인

## 개요

```
POST /retrieve  →  [1] 쿼리 라우팅 → 검색 → 리랭킹 → 응답
POST /answer    →  [1] 쿼리 라우팅 → [2] CRAG 루프 → [3] 프롬프트 분기 → LLM → [4] Self-RAG → 응답
```

`/retrieve`는 1단계만 적용, `/answer`는 1~4단계 전체 적용.

## 단계별 실행 주체 (누가 무엇을 하는가)

같은 `/answer` 호출이라도 각 단계의 실행 주체가 다르다. 가장 혼동되기 쉬운 부분 — LLM이 호출되는 곳과 아닌 곳을 한 표로:

| 단계 | 실행 주체 | LLM? |
|---|---|---|
| [1-A] 쿼리 유형 분류 (5-type) | 정규식 | ❌ 0ms |
| [1-B] Dense/BM25 배수 선택 | 분류 결과 기반 dict lookup | ❌ |
| [1-C] COMPARISON 분해 (1차) | 정규식 `_PAIR_PATTERN` | ❌ 0ms |
| [1-C] COMPARISON 분해 (2차 fallback) | vLLM (Qwen3) | ✓ ~2s |
| Dense 검색 | BGE-M3 + Qdrant ANN | 임베딩 GPU |
| BM25 검색 | Qdrant sparse | ❌ |
| RRF 융합 | Qdrant 내부 | ❌ |
| CrossEncoder 리랭킹 | BGE-reranker-v2-m3 | 리랭커 GPU |
| [2] CRAG 점수 평가 | 숫자 비교 (`score >= 0.3`) | ❌ |
| [2] CRAG 쿼리 재작성 (필요 시) | vLLM (Qwen3) | ✓ ~2s |
| [3] 프롬프트 템플릿 선택 | dict lookup (`PROMPTS[query_type]`) | ❌ |
| [3] 답변 생성 | vLLM (Qwen3) | ✓ ~3s |
| [4] Self-RAG 검증 (조항·숫자 구조) | 정규식 + 집합 비교 | ❌ |
| [4'] Critic failure type 분류 | 정규식 + 집합 비교 + (선택) NLI/LLM judge | ❌ (judge 미주입 시) |
| [4'] Hint-guided regenerate (조건부) | vLLM, generation_error/unit_error에만 1회 | ✓ ~3s (조건부) |

**LLM이 호출되는 지점은 4곳** — 분해 fallback · CRAG 재작성 · 답변 생성 · Critic regenerate(조건부). **분류·선택·검증은 전부 결정론적** (정규식 or dict lookup or 숫자 비교). 그래서 **라우팅·프롬프트 선택·검증·failure type 분류는 매 요청 0ms**로 끝난다.

## [1] 쿼리 라우팅 (Rule 기반)

**쿼리를 읽고 → 성격 분류 → 성격에 맞춘 검색 전략 실행** 의 3단계 세트로 작동. 3개가 맞물려 하나의 Adaptive 레이어를 이룬다. **이 단계는 1-C의 2차 fallback을 제외하면 전부 정규식 — LLM 호출 없음.**

### 1-A. 5-type regex classifier — 쿼리 성격 판정

| 유형 | 감지 패턴 | 예시 |
|---|---|---|
| `STRUCTURED_LOOKUP` | 제N조 / 별표N / 부칙 / 서식N (의미 질문 없이) | "제43조 내용 알려줘" |
| `INTERPRETATION` | 해석 · 의미 · 적용 · 되나요? · 가능한가요? | "무면허운전 시 보장되나요?" |
| `PROCEDURE` | 어떻게 · 방법 · 절차 · 신청 · 청구 | "보험금 청구 방법은?" |
| `COMPARISON` | 비교 · 차이 · 다른 점 · vs | "1종과 2종의 차이가 뭔가요?" |
| `SIMPLE_FACT` | 위 4개 다 해당 안 될 때 (기본값) | "보험금 지급 기준" |

분류 결과로 **(1) 하이브리드 검색 배수**와 **(2) 프롬프트 템플릿**이 결정됨.

### 1-B. Dense/BM25 factor 분기 — 후보 풀 구성 조절

검색 구조는 **항상 하이브리드** (Dense BGE-M3 + BM25 → RRF 융합). 다만 각 검색이 **prefetch하는 후보 수 배수**를 쿼리 유형에 맞춰 조절한다.

```python
Prefetch(query=dense_vec,  using="dense",     limit=top_k * dense_factor)
Prefetch(query=bm25_text,  using="content-bm25", limit=top_k * bm25_factor)
# → RRF 융합
```

| 전략 | dense | bm25 | 적용 유형 | 이유 |
|---|---|---|---|---|
| **BM25_HEAVY** | ×3 | **×8** | `STRUCTURED_LOOKUP` | "제43조"는 의미보다 **키워드 정확 매칭**이 결정적. BM25가 토큰 일치로 정확. Dense는 보조 |
| **DENSE_HEAVY** | **×8** | ×3 | `INTERPRETATION` · `PROCEDURE` · `COMPARISON` | "무면허운전 시 보장되나요?"는 **의미 이해**가 중요. Dense 벡터가 유리. BM25는 보조 |
| **HYBRID** | ×6 | ×6 | `SIMPLE_FACT` · 조항+의미 혼합 | 판단 불가 → 균등 |

즉 같은 `top_k=10` 요청이라도:
- STRUCTURED_LOOKUP: BM25에서 80개, Dense에서 30개 prefetch → 융합 후 BM25 비중 높음
- INTERPRETATION: Dense에서 80개, BM25에서 30개 → Dense 비중 높음

**검색 방식을 바꾸는 게 아니라 후보 풀의 구성을 바꾸는 방식**. Cormack et al. *RRF* (SIGIR 2009)의 원리 — 어느 쪽 리스트에서 더 많은 후보가 들어오면 fusion 결과도 그쪽 비중이 커짐.

### 1-C. COMPARISON Query Decomposition — 비교 쿼리 분해

`COMPARISON` 유형만 추가 단계. 단일 검색으로 "1종과 2종의 차이"를 벡터 검색하면 두 대상을 동시에 표현하려 해서 어느 쪽도 제대로 못 잡음. **서브쿼리 2개로 쪼개서 각각 검색 → 합산 → 원 쿼리로 리랭킹**.

```
"1종과 2종의 차이가 뭔가요?"
  ↓ 분해 (아래 3단계)
  ["1종 차이", "2종 차이"] 서브쿼리 2개
  ↓ 각각 Dense 8× / BM25 3× prefetch + RRF
  chunks_from_1종 ∪ chunks_from_2종 (중복 제거)
  ↓ 원 쿼리 "1종과 2종의 차이가 뭔가요?"로 CrossEncoder 1회 리랭킹
  최종 top_k
```

**3단계 분해 전략** (fallback chain):

1. **규칙 기반** (1차, latency ~0ms) — `_PAIR_PATTERN` 정규식, 2전략
   - 같은 접미어 공유: `형·종·안·판·급·타입·모드·방식·유형·단계·레벨·버전·수준` 13종 중 하나가 양쪽에 붙는 경우 (예: "1종과 2종", "기본형과 고급형", "초급과 중급")
   - 명시적 구분자: `vs` / `versus` / `대` (예: "Python vs Java", "A 대 B")
   - 도메인별 접미어 확장이 필요하면 `src/v1/rag/classifier.py`의 `_PAIR_SUFFIXES` 튜플에 추가 (예: 보험 도메인 "용·플랜·병원·일당")
2. **LLM 기반** (2차, fallback, +~2s) — `DECOMPOSE_PROMPT`로 LLM이 MULTI/SINGLE 판단
   ```
   질문: 자가용과 영업용 운전자의 보장 범위 차이는?
   MULTI: [자가용운전자 보장 범위, 영업용운전자 보장 범위]
   ```
3. **둘 다 실패** → 단일 검색 폴백 + trace에 `method=llm_failed` 기록

**first-wins 원칙**: 초기 호출에서만 `rec.decomposition`을 기록. CRAG 재시도의 rewritten query가 원본 결과를 덮어쓰지 않도록.

### 세 단계가 맞물리는 전체 흐름

```
"1종과 2종의 차이가 뭔가요?" 입력
  ↓ (1-A) regex classifier
  query_type = COMPARISON
  ↓ (1-B) factor 결정
  dense=8, bm25=3 (DENSE_HEAVY)
  ↓ (1-C) decomposition
  ["1종 차이", "2종 차이"] 서브쿼리
  ↓ 서브쿼리마다 Dense 8× / BM25 3× prefetch + RRF
  ↓ 합산 후 원 쿼리로 리랭킹
  최종 top_k chunks → [2] CRAG 루프로
```

### 구현 위치

| 책임 | 파일 : 함수/상수 |
|---|---|
| 5-type 분류 (regex) + factor 결정 | [src/v1/rag/classifier.py](../src/v1/rag/classifier.py): `classify_query()`, `RouteResult`, `QueryType`, `SearchStrategy`, `_STRUCTURED_REF_PATTERN` / `_PROCEDURE_PATTERN` / `_COMPARISON_PATTERN` / `_INTERPRETATION_PATTERN` (BASE + DOMAIN extension 구조) |
| COMPARISON 분해 (1차 regex) | [src/v1/rag/classifier.py](../src/v1/rag/classifier.py): `decompose_comparison()`, `_PAIR_PATTERN` |
| COMPARISON 분해 (2차 LLM fallback) | [src/v1/rag/search.py](../src/v1/rag/search.py): `decompose_query_llm()` + [src/v1/rag/prompts.py](../src/v1/rag/prompts.py): `DECOMPOSE_PROMPT` |
| 서브쿼리 오케스트레이션 (first-wins 포함) | [src/v1/rag/search.py](../src/v1/rag/search.py): `search_comparison()` |
| 하이브리드 검색 (Dense + BM25 + RRF) | [src/v1/rag/search.py](../src/v1/rag/search.py): `search_rrf_only()` |
| 리랭킹 포함 검색 | [src/v1/rag/search.py](../src/v1/rag/search.py): `search_and_rerank()` |
| prefetch 배수 상수 | [src/v1/config/settings.py](../src/v1/config/settings.py): `SEARCH_PREFETCH_MULTIPLIER` |

**관측 메모**:
- 쿼리 유형 분포(INTERPRETATION / SIMPLE_FACT / COMPARISON 등)는 평가셋 vs 운영 데이터 간 차이가 클 수 있으므로 `trace_summary.py` Section 1으로 주기 확인
- Decomposition rule hit rate 낮은 도메인은 `_PAIR_PATTERN` 커버리지 밖 — 패턴 확장 또는 LLM 분해 단일화 검토 지점
- 서브쿼리 post-processing(조사·종결어미 제거) 미흡 시 약한 서브쿼리 → 초기 검색 실패 → CRAG 재시도 도미노 가능

## [2] CRAG 루프 (검색 품질 평가 → 재검색)

CrossEncoder 리랭킹 후 **상위 문서의 rerank score**로 검색 품질을 판단한다. **판단 자체는 숫자 비교(`score >= 0.3`), LLM 호출 없음**. 판단 결과 "부족"일 때만 쿼리 재작성을 위해 LLM 1회 호출.

```
검색 → 리랭킹 → score >= 0.3? ─Yes→ 다음 단계
                    │
                    No
                    │
               LLM 쿼리 재작성 → 라우팅 다시 수행 → 재검색 (최대 2회)
                    │
               그래도 부족 → "관련 내용을 찾지 못했습니다" 반환
```

- **판단 기준**: `CRAG_SCORE_THRESHOLD = 0.3`
- **재작성**: vLLM에 "검색에 더 적합한 형태로 재작성하라" 프롬프트
- **재작성 쿼리도 라우팅 재수행**: 원래 쿼리는 STRUCTURED_LOOKUP이었는데 재작성 후 INTERPRETATION이 될 수 있음
- **최대 재시도**: `CRAG_MAX_RETRIES = 2`
- **COMPARISON 유지**: 재작성 후에도 COMPARISON이면 Query Decomposition 재적용

### 구현 위치

| 책임 | 파일 : 함수/상수 |
|---|---|
| 점수 게이트 판정 (숫자 비교) | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `evaluate_retrieval()` |
| LLM 쿼리 재작성 | [src/v1/rag/search.py](../src/v1/rag/search.py): `rewrite_query()` + [src/v1/rag/prompts.py](../src/v1/rag/prompts.py): `REWRITE_PROMPT` |
| 재시도 루프 orchestration | [src/v1/router.py](../src/v1/router.py): `answer()` 내부 `while not evaluate_retrieval(...)` 블록 |
| threshold / 최대 재시도 상수 | [src/v1/config/settings.py](../src/v1/config/settings.py): `CRAG_SCORE_THRESHOLD`, `CRAG_MAX_RETRIES` |

**알려진 한계**:
- 최악 케이스에서 LLM 호출 5~6회(분해 1 + 재작성 2 + 답변 1), latency 10초+
- CRAG on/off A/B 비교 데이터 미확보. 관측 지표: `crag_retry_rate`, `crag_on_off_ab_test`
- Query Decomposition 규칙 매칭률 미측정. 관측 지표: `qd_rule_hit_rate`, `qd_llm_hit_rate`, `qd_single_rate`

## [3] 프롬프트 분기

쿼리 유형은 **1단계 정규식 분류에서 이미 결정됨**. 여기는 단순 `PROMPTS[query_type]` dict lookup으로 템플릿만 꺼내 오는 단계 — **LLM 호출 없음**. 실제 LLM 호출은 선택된 템플릿에 context·query를 채워 넣은 뒤 **답변 생성 시점**에 1회 발생.

| 유형 | 프롬프트 전략 |
|------|-------------|
| STRUCTURED_LOOKUP | 해당 참조(조항·섹션·표 등)의 원문 정확 인용 + 구체적 위치(장·절·챕터) 명시 |
| INTERPRETATION | IRAC 구조 (쟁점→규정→적용→결론). 근거 조항 명시 |
| PROCEDURE | 절차를 단계별 설명. 각 단계의 근거 조항 명시 |
| COMPARISON | 비교 항목을 표 형식으로 정리. 근거 조항 명시 |
| SIMPLE_FACT | 간결 답변 + 근거 조항 명시 |

공통: 모든 프롬프트에 "아래 컨텍스트만 참고하여" 제약 포함.

### 구현 위치

| 책임 | 파일 : 함수/상수 |
|---|---|
| 프롬프트 템플릿 5종 | [src/v1/rag/prompts.py](../src/v1/rag/prompts.py): `PROMPTS` dict (QueryType enum을 키로) |
| 공통 규칙 상수 | [src/v1/rag/prompts.py](../src/v1/rag/prompts.py): `_COMMON_RULES` |
| 템플릿 선택 + LLM 호출 | [src/v1/router.py](../src/v1/router.py): `answer()` 내 `prompt = PROMPTS[route.query_type]` → `llm.invoke(prompt.format_messages(...))` |
| LLM 클라이언트 | [src/v1/rag/clients.py](../src/v1/rag/clients.py): `llm = ChatOpenAI(...)` 싱글톤 + [src/v1/config/](../src/v1/config) `LLM_CONFIG` (vLLM base_url + Qwen3 모델 경로) |
| 컨텍스트 토큰 예산 | [src/v1/rag/tokens.py](../src/v1/rag/tokens.py): `calc_context_budget()`, `truncate_context()`, `count_tokens()` |

## [4] Self-RAG 검증

LLM 답변에서 구조적 참조(조항·별표·숫자)를 추출해 context와 대조하고 위험 등급을 부여한다. **전 과정이 정규식 + 집합 비교(set difference) + 규칙 기반 policy gate — LLM 호출 없음**. 검증 자체는 **결정론적**이라 같은 (답변, context) 쌍은 항상 동일 `risk_level`을 리턴.

이름에 "Self-RAG"가 들어가 있지만 원논문(Asai et al., ICLR 2024)의 reflection token 기반 self-critique와는 다르다. **Self-RAG의 `[ISSUP]` 토큰이 잡으려던 "의미 일치" 검증**(예: 답변 "보장된다" vs context의 "보장하지 아니한다")**은 현재 미구현** — 구조적 참조 존재·부재만 본다. 의미 검증은 NLI/HHEM 어댑터 확장 지점 (`semantic_judge` 슬롯).

### 검증 대상

| 유형 | 예시 | 처리 |
|---|---|---|
| 조항 참조 | "제12조 제3항 제1호" | 계층형 파싱 (`ArticleRef`) — 조항만 맞고 항이 틀린 케이스도 분리 |
| 별표/부칙/서식 | "별표1", "부칙 제2조", "서식3" | `AppendixRef` |
| 금액·기간·비율·나이 | "1,000만원", "90일", "10%" | 단위 정규화 (`1,000만원` == `10,000,000원`) |
| 날짜 | "2026.04.20" | 구분자 정규화 후 비교 |

답변을 claim 단위(한국어 종결 어미 기준 단순 분할)로 쪼개 각 claim의 구조적 fact를 추출한다. Chunk-level provenance — 어느 chunk가 그 claim의 근거인지 — 는 **trace JSONL에만 기록**하고 응답에는 노출하지 않는다. UI citation이 필요해지면 확장 지점으로 사용.

### 위험 등급 (severity 축)

`verify_answer`가 매기는 **severity** 축(`risk_level`). root cause(`failure_type`)와 control flow(`action_taken`)는 별개 축으로, 다음 섹션에서 다룸.

| severity (`risk_level`) | 조건 | 권장 처리 |
|---|---|---|
| `hard_fail` | 존재하지 않는 조항/별표/부칙 참조 | 답변 재생성 / CRAG 재트리거 / 사용자 알림 |
| `soft_fail` | 금액·기간·퍼센트 mismatch (크리티컬 단위) | "근거 문서에 확인되지 않음" 주석 |
| `warn` | 나이·횟수 등 사소한 숫자 차이 (비크리티컬 단위) | 답변 반환 + 로그 |
| `pass` | 모두 일치 | 답변 반환 |

**Critic dispatch** (기본 off · opt-in — 실측 hard_fail 대부분 오탐이라 auto-regenerate 무의미, [design-retrospective §1.5](design-retrospective.md)): `/answer`는 위험 등급뿐 아니라 **failure type**으로 분류해서 분기한다. risk_level이 hard_fail/soft_fail이면 critic dispatch가 발동해 5분류로 나눈 뒤 generation_error/unit_error만 hint-guided regenerate (1회 제한). retrieval_gap/semantic_mismatch는 **regenerate 금지** + escalation 플래그 (Huang et al. ICLR 2024 함정 회피).

### Failure type 분류 (Critic dispatch)

**root cause** 축(`critic.failure_type`) 정의. severity(`risk_level`) 및 control flow(`critic.action_taken`)와는 별도 축.

| root cause (`failure_type`) | 판정 방법 | control flow (`action_taken`) | 응답 플래그 |
|---|---|---|---|
| `generation_error` | missing_ref가 context의 ±1 인접 조항/항과 매치 (인접 번호 착각) | `regenerate` (hint-guided, 1회 제한) | regenerate 성공 시 verification 필드 갱신 |
| `retrieval_gap` | missing_ref가 context 어떤 참조와도 분리 | `escalate` (**regenerate 금지** — 재생성해도 못 고침) | `verification.escalation_required: true` |
| `unit_error` | 같은 canonical unit 내 수치 근접 (proximity < 50%, KRW/DAYS/YEARS/PERCENT) | `regenerate` (hint-guided, 1회 제한) | regenerate 성공 시 verification 필드 갱신 |
| `semantic_mismatch` | LLM judge 주입 시만 (기본 비활성) | `escalate` (regenerate 금지) | `verification.escalation_required: true` |
| `minor` | 나이·횟수 등 비크리티컬 단위 mismatch, 또는 크리티컬이지만 거리가 멀어 회복 불가 | `pass` (기존 경고만 노출) | 기존 `warnings` 유지 |

Hint 텍스트는 [src/v1/rag/grader.py](../src/v1/rag/grader.py) `build_hint()`가 context의 허용 참조/수치 목록 + 금지 항목으로 생성. Regenerate 프롬프트는 [src/v1/rag/prompts.py](../src/v1/rag/prompts.py) `REGENERATE_WITH_HINT_PROMPT`.

### 실제 연결된 처리 (3축 좌표별)

각 행은 (severity, root cause, control flow) 좌표 1개. 답변은 모든 케이스에서 반환되므로 별도 컬럼 생략.

| severity (`risk_level`) | root cause (`failure_type`) | control flow (`action_taken`) | 로그 | trace.critic 기록 | 응답 `verification` |
|---|---|---|---|---|---|
| `hard_fail` | `generation_error` | `regenerate` | ⚠ warning + regenerate 로그 | `{invoked, failure_type, action_taken=regenerate, ...}` | warnings 노출, regenerate 후 갱신 |
| `hard_fail` | `retrieval_gap` | `escalate` | ⚠ warning + escalation 로그 | `{invoked, action_taken=escalate, ...}` | warnings + `escalation_required: true` |
| `soft_fail` | `unit_error` | `regenerate` | ⚠ warning + regenerate 로그 | `{invoked, action_taken=regenerate, ...}` | warnings 노출, regenerate 후 갱신 |
| `soft_fail` | `minor` | `pass` | ⚠ warning | `{invoked, failure_type=minor, action_taken=pass, ...}` | warnings 노출 (원본 그대로) |
| `warn` | (미기록 — critic 미호출) | (미호출) | 없음 | `{invoked: false}` | warnings 있으면 노출 |
| `pass` | (미기록 — critic 미호출) | (미호출) | 없음 | `{invoked: false}` | 필드 생략 |

집계는 [scripts/trace_summary.py](../scripts/trace_summary.py) Section 10 `Critic Dispatch` (invocation rate / failure_type 분포 / regenerate_improved_rate / risk 전환 패턴 / **critic-passed origins**).

#### Trace를 읽는 3축 (multi-layer guardrails 표준)

verification 결과는 단일 status가 아니라 **3개의 직교 축**으로 trace에 기록된다. 같은 이름(`pass`)이 여러 축에 나오는 게 정상 — 어느 축인지를 보고 해석한다.

| 축 | 값 | 출처 |
|---|---|---|
| **severity** (`risk_level`) | `pass` / `warn` / `soft_fail` / `hard_fail` | [`verify_answer`](../src/v1/rag/grader.py) |
| **root cause** (`critic.failure_type`) | `minor` / `retrieval_gap` / `generation_error` / `unit_error` / `semantic_mismatch` | [`classify_failure`](../src/v1/rag/grader.py) (critic 호출 시만 trace에 기록) |
| **control flow** (`critic.action_taken`) | `regenerate` / `escalate` / `pass` (+ `critic.invoked=false`인 미호출 케이스) | router.py critic dispatch |

`warn`과 `minor`는 **다른 축** — 경쟁 관계 아님. 헷갈리기 쉬운 두 케이스를 3축 좌표로 표현하면:

| 케이스 | risk_level | critic.invoked | failure_type | action_taken | 사용자 응답 |
|---|---|---|---|---|---|
| **silent warn** | `warn` | `false` | (미기록) | (미기록) | warnings 노출 |
| **critic-passed minor** | `soft_fail` | `true` | `minor` | `pass` | warnings 노출 |

응답(답변 + warnings)은 동일하지만 trace 흔적이 달라 운영 신호로 분리 가능. `critic-passed origins`(Section 10)에서 `soft_fail` 비중 ↑ = mismatch는 발생하는데 거리가 멀어 hint-guided regenerate로 못 고침 → **retrieval 품질 의심**.

> hard_fail + minor 조합은 발생 불가 — `classify_failure`가 missing_refs 있으면 무조건 `generation_error` / `retrieval_gap`으로 분기하므로 ([grader.py:348-356](../src/v1/rag/grader.py#L348-L356)) `minor`는 missing_refs 없는 경로에서만 도달.

### 반환 스키마 (응답)

```json
{
  "risk_level": "hard_fail",
  "groundedness": 0.50,
  "warnings": ["context에 없는 조항 참조: 제99조"],
  "escalation_required": true
}
```

`groundedness`는 0~1 스칼라 (`supported / verifiable`) — RAGAS faithfulness · Azure AI Foundry Groundedness 패턴. **검증 가능한 claim(조항/별표/숫자가 추출된)만 분모**에 둠 — 평문 claim ("이 경우 보험금이 지급됩니다")은 구조적으로 supported_by_chunks가 강제 [] 라 분모에 넣으면 절차/해석형 답변이 부당하게 0점으로 깔리는 분모 결함이 생김. verifiable claim이 0 (순수 평문 답변)이면 키 자체 생략 — 측정 불가 ⇒ 평균 왜곡 방지. risk_level의 4단계 라벨이 못 보여주는 추세·A/B 비교·SLA 임계 박는 데 사용.

`escalation_required`는 retrieval_gap / semantic_mismatch에서만 노출 (regenerate 안 함을 명시). 클라이언트가 재질문 유도·refusal UI 등으로 활용 가능.

응답에는 추가로 `citations[]` 배열이 들어감 (claim별 supported_by_chunks 매핑이 있을 때만). 형태: `{"claim", "refs": ["제43조"], "supported_by_chunks": ["chunk_id..."]}`. 클라이언트가 `sources[].chunk_id`와 lookup해서 inline `[1][3]` UI 구성 가능 — Anthropic Citations API · Perplexity 패턴.

claim 단위 상세(`claims[]`·`missing_refs`·`numeric_mismatches`·`supported_by_chunks`)는 내부 계산 후 `data/eval/trace/<YYYYMMDD>/traces.jsonl`에 기록. critic 동작 (`critic.failure_type`, `critic.regenerate_improved` 등)도 같은 trace에 함께 저장.

### 알려진 한계 (확장 지점)

| 한계 | 예시 | 확장 후보 |
|---|---|---|
| 의미 반전 미감지 | context "보장하지 아니한다" + 답변 "보장된다" → pass | NLI classifier (HHEM-2.1) / LLM judge |
| Parametric 지식 주입 | LLM이 일반 상식으로 보강한 문장 | 동일 |
| Temporal mismatch | 개정 전 조항 참조 | 개정 이력 대조 레이어 |
| Claim 과분할 | 한국어 종결어미 기준 단순 split — 복문 부정확 | LLM 기반 분해 (FActScore 스타일) |

### 구현 위치

| 책임 | 파일 : 함수/상수 |
|---|---|
| 오케스트레이션 (답변·context 대조 + 위험 판정) | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `verify_answer()` |
| 조항·별표·숫자·날짜 추출 (정규식) | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `extract_article_refs()`, `extract_appendix_refs()`, `extract_numeric_facts()` + `_ARTICLE_HIERARCHY_RE`, `_APPENDIX_RE`, `_NUMBER_SPAN_RE`, `_DATE_RE`, `_NUMERIC_UNIT_MAP` |
| 구조적 참조 dataclass | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `ArticleRef`, `AppendixRef`, `NumericFact`, `Chunk` |
| Claim 분해 (한국어 종결어미 기준) | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `decompose_claims()`, `_CLAIM_SPLIT_RE` |
| Claim-chunk 근거 매핑 | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `_provenance_map()`, `_build_claim_record()` |
| 위험 등급 정책 게이트 | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `_decide_risk()`, `_BUSINESS_CRITICAL_UNITS` |
| 사람이 읽는 경고 메시지 빌드 | [src/v1/rag/grader.py](../src/v1/rag/grader.py): `_build_warnings()` |
| 호출 + trace 기록 + 응답 projection | [src/v1/router.py](../src/v1/router.py): `answer()` 내 `verify_answer()` 호출 → `rec.verification` slim count → warning 로그 → `result["verification"]` 2키 projection |

### 설계 참조

Self-RAG (Asai et al., ICLR 2024) · FActScore (Min et al., EMNLP 2023) · AIS (Rashkin et al., 2023) · Anthropic Citations API · Azure Groundedness Detection · RAGAS FaithfulnessWithHHEM

## 응답 구조

```json
{
  "trace_id": "abc-123-...",
  "query": "제43조 보험금 지급 기준이 뭐야",
  "answer": "...",
  "elapsed_ms": 2340,
  "sources": [
    {"chunk_id": "121", "page_range": [15, 15], "content": "...", "chunk_type": "text", "rrf_score": 0.0312, "rerank_score": 0.8721},
    {"chunk_id": "122", "page_range": [15, 15], "content": "...", "chunk_type": "image", "rrf_score": 0.0280, "rerank_score": 0.7510, "image_paths": ["약관_images/img8.png"]}
  ],
  "citations": [
    {"claim": "제43조에 따라 보험금이 지급된다", "refs": ["제43조"], "supported_by_chunks": ["121"]}
  ],
  "route": {"strategy": "hybrid", "query_type": "interpretation"},
  "verification": {"risk_level": "warn", "groundedness": 0.83, "warnings": ["..."]},
  "crag_retries": 1
}
```

| 필드 | 조건 | 설명 |
|------|------|------|
| `route` | 항상 | 라우팅 결과 (검색 전략 + 쿼리 유형) |
| `sources[].chunk_id` | 항상 | Qdrant point ID — `citations[].supported_by_chunks` 매핑 키 |
| `citations` | claim에 ref 매핑된 게 있을 때만 | claim별 인용 매핑 (Anthropic Citations API 패턴) |
| `verification` | warnings 있을 때만 | Self-RAG + Critic 결과 (`risk_level`, `groundedness` 0~1, `warnings`, optional `escalation_required`) |
| `crag_retries` | 재검색 발생 시만 | CRAG 재시도 횟수 |

## 튜닝 포인트

| 파라미터 | 위치 | 현재값 | 조절 방향 |
|---------|------|--------|----------|
| CRAG threshold | config/settings.py | 0.3 | 올리면 재검색 빈번, 내리면 저품질 허용 |
| CRAG max retries | config/settings.py | 2 | 올리면 latency↑ 품질↑. 1회당 +2~3초 (vLLM 재작성 + 재검색 + 리랭킹) |
| SIBLING_WINDOW | config/settings.py | 2 | hit 기준 ±N개 sibling 복원 |
| SEARCH_PREFETCH_MULTIPLIER | config/settings.py | 3 | RRF prefetch top_k의 N배 |
| dense/bm25 factor | rag/router.py | 3~8 | 검색 전략별 prefetch 배수 |
| 라우팅 정규식 | rag/router.py | 현재 패턴 | 도메인 확장 시 패턴 추가 |
| 프롬프트 템플릿 | rag/prompts.py | 현재 5종 | 답변 품질 보고 조절 |
