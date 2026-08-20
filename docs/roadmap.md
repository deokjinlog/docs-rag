# 모델 고도화 로드맵 (측정 → 조건부 파인튜닝)

> 이 문서는 **아직 구현되지 않은** 확장 방향의 설계·근거·트리거 조건이다. 실제 학습은 GPU 서버에서 수행하며, README의 *"확장 지점"* · *"의도적 미구현"* 과 같은 원칙 — **측정된 이득만 메인 경로에** — 을 따른다. 트리거 조건이 충족되기 전에는 도입하지 않는 게 기본값(default = 미도입).

세 축을 다룬다: **① BGE-M3 대조학습 임베딩 파인튜닝 · ② Qwen3 LoRA 도메인 어댑터 · ③ RAGAS/Retrieval 평가 확장**. 다만 **순서가 아니라 트리거 게이트**다 — ③(측정)이 선행 필수이고, 그 측정 결과가 ①이냐 ②냐를 가른다.

---

## 0. 왜 "측정 먼저"인가

파인튜닝은 비싸고(재임베딩·Qdrant 재인덱싱·서빙 변경) 되돌리기 어렵다. **이득이 측정되지 않은 파인튜닝 = dead infrastructure + 신뢰도 하락 위험** — README "검증 안 된 컴포넌트 추가 = 신뢰도 깎기" 원칙의 학습판이다.

핵심은 **현재 무엇이 병목인지 데이터로 모른다**는 것이다. 평가 스냅샷을 보면:

| 신호 | 값 | 해석 |
|---|---|---|
| Context Utilization | **0.92** (높음) | 검색된 context를 답변이 잘 활용 |
| Faithfulness | **0.69** (중간) | 그런데 근거 준수는 중간 |
| Critic regenerate improved | **14.3%** (낮음) | hint-guided 재생성이 잘 안 고침 |

이 조합은 **retrieval-bound가 아니라 generation-bound** 쪽 신호일 수 있다 (검색은 되는데 생성이 context를 못 지킴). 하지만 확정하려면 **Context Recall / Retrieval Recall@k** 를 측정해야 한다 — 현재는 golden chunk 라벨링 비용 때문에 보류 상태(Context Utilization이 proxy). 이 공백을 메우는 게 Phase 0다.

```
Phase 0 (측정 기반 구축, 선행 필수)
        │
        ├── retrieval-bound 판정 ──▶ Phase 1 (임베딩 대조학습)
        │   (Context Recall / Recall@k 낮음, 도메인 용어 쿼리 취약)
        │
        └── generation-bound 판정 ─▶ Phase 2 (LoRA 어댑터)
            (근거는 검색됐는데 Faithfulness 낮음 + generation_error 다발)
```

---

## 1. Phase 0 — 측정 기반 (파인튜닝의 게이트)

### 1.1 추가할 지표

| 축 | 현재 | 추가 | 목적 |
|---|---|---|---|
| RAGAS | Faithfulness / Answer Relevancy / Context Utilization | **Context Precision · Context Recall** | 검색 정밀도/재현율 분리 → retrieval vs generation 병목 분해 |
| Retrieval | ✅ Recall@k·MRR (`eval_retrieval.py`, recall@5=1.0·MRR=0.86·n=25) | nDCG@k | 검색 품질 절대치. 파인튜닝 전/후 비교 기준선 |
| 병목 분해 | ✅ **판별식 구현** (`diagnose_bottleneck.py`, `make diagnose`) | 도메인용어 쿼리 세분 recall | retrieval 충분+generation 약함 → generation-bound. 입력 품질(nan·편향 judge)까지 자가 채점 |

**현재 판정(`make diagnose`, 2026-08-13)**: `generation-leaning (잠정)` — retrieval는 충분
(recall@5=1.0 → 정답 청크가 top-k에 듦, 검색 병목 배제)하나, 저장된 RAGAS가 **faithfulness=nan +
self-judge(편향)** 이라 생성측을 확정 못 함. **게이트 BLOCKED** → §1.3의 비편향 judge(GPT-4o-mini)로
`eval_ragas` 재측정해 faithfulness를 확정해야 Phase 2 트리거가 열린다. 판정 근거는
`data/eval/bottleneck_verdict.json` 에 보존(precision-first: nan/편향 데이터로 확정 판정을 지어내지 않음).

**세그먼트 분해가 Phase 1 가설을 반증(`eval_retrieval --segment`)**: §2.1은 "도메인 용어 쿼리가
일반보다 recall 낮으면 retrieval-bound → Phase 1"을 트리거로 뒀다. 실측은 **정반대** — 도메인 어휘
질의(n=19: 중환자실·소득보장수술·특약…)가 recall@1=0.842·MRR=0.921로, 일반 질의(n=6: 0.500·0.672)
보다 **더 잘** 검색된다(recall@5는 집계 1.0의 귀결로 둘 다 1.0 → 판별은 미포화 축인 @1·MRR). 변별력
있는 약관 용어가 강한 검색 앵커라 오히려 일반적 표현이 여러 청크와 경쟁해 순위가 흐려지는 것. →
**Phase 1(도메인 임베딩 대조학습) 트리거는 충족과 더 멀어졌다**(도메인이 약점이 아니라 강점). 단
일반 세그먼트 n=6은 작아 방향 신호로만(재라벨·확장은 §1.2).

### 1.2 평가셋 — 코퍼스 마이닝으로 silver golden

golden chunk 라벨링(도메인 전문가 3~6h)을 우회하고 **기존 코퍼스에서 pseudo-golden을 마이닝**한다:

- **reverse-QA**: 청크 C → LLM이 "C로 답할 수 있는 질문" 생성 → `(질문, C)` 가 golden pair. 사람 라벨 없이 Recall@k 측정 가능(silver).
  - 한계: LLM 생성 질문 분포 ≠ 실제 사용자 질문. → **운영 trace의 실제 질문**을 섞어 보정(hybrid), 소량은 사람 spot-check로 gold 승격.
- **기존 24문항 확장**: `data/eval` 의 라벨을 trace 실질문 + spot-check로 점증 확장.

### 1.3 A/B 하네스 (before/after)

- 평가셋 **freeze** → `baseline`(off-the-shelf) vs `candidate`(fine-tuned) 를 **동일 입력·동일 judge**(GPT-4o-mini — serving과 분리해 self-preference bias 회피, 기존 `eval_ragas.py` 정책 유지) 로 비교.
- **채택 게이트**: candidate가 held-out에서 유의미 개선 **AND** 일반 쿼리 회귀 없음. (§4 공통 게이트)

### 1.4 산출 = 트리거 신호

Phase 0가 다음 분기를 만든다. **여기서 나온 숫자가 없으면 Phase 1/2로 못 넘어간다.**

| 측정 결과 | 판정 | 다음 |
|---|---|---|
| Context Recall / Recall@k 목표 미달, 특히 도메인 용어(특약·별표·조항 표기) 쿼리에서 baseline이 일반 쿼리보다 유의미하게 낮음 | **retrieval-bound** | → Phase 1 |
| 검색 근거는 충분(Context Recall 높음)한데 Faithfulness / Answer Relevancy 낮음 + critic `generation_error` 비율 높음 | **generation-bound** | → Phase 2 |

**구현 위치**: ✅ `scripts/eval_retrieval.py`(recall@k·MRR) · ✅ `scripts/diagnose_bottleneck.py`(판별식,
`make diagnose`) · (예정) `scripts/eval_ragas.py` Context Precision·Recall 확장 · (예정)
`scripts/build_eval_set.py`(reverse-QA 마이닝). 판별식은 두 baseline(retrieval·ragas)만 읽어 스택
없이 돌고, 입력 품질(nan·편향 judge)을 스스로 채점해 확정/잠정을 가른다.

---

## 2. Phase 1 — BGE-M3 대조학습 임베딩 파인튜닝 (조건부: retrieval-bound)

### 2.1 트리거

Phase 0에서 **retrieval-bound** 판정 + 도메인 용어 쿼리가 일반 쿼리보다 Recall 유의미하게 낮음. (트리거 미충족 시 미도입.)

> **현재 트리거 미충족 — 오히려 반증됨**: `eval_retrieval --segment`(2026-08-13) 실측상 도메인 어휘
> 질의가 일반보다 **더** 잘 검색됨(recall@1 0.842 vs 0.500·MRR 0.921 vs 0.672). 도메인 용어 열위가
> Phase 1의 전제인데 반대 신호가 나왔으므로, 임베딩 대조학습은 현 데이터상 근거 없음. 재측정으로
> 도메인 열위가 확인될 때만 재검토(§1.4 판별식이 자동 감시).

### 2.2 방법

- **InfoNCE 대조학습**, `(query, positive, hard_negatives)` 삼중항. FlagEmbedding(BGE-M3 공식) 또는 sentence-transformers.
- **dense 1024차원 유지 필수** — Qdrant 컬렉션·`content-bm25`·`embed.py`/`qdrant.py` 차원 계약(CLAUDE.md 연쇄수정). 차원 바뀌면 전면 재인덱싱.
- **LoRA(PEFT)로 encoder 파인튜닝** 우선 — 가볍고 되돌리기 쉬움(full-param 대비).
- **리랭커(`bge-reranker-v2-m3`) 파인튜닝을 먼저** 검토 — cross-encoder는 bi-encoder보다 precision ROI가 큰 경우가 많고 재인덱싱이 불필요. 저비용 순서: **Phase 1a(리랭커) → Phase 1b(임베더)**.

### 2.3 데이터 — 코퍼스 마이닝

| 종류 | 소스 | 비고 |
|---|---|---|
| **positive** | (a) 구조적: 같은 `heading_path`·sibling(`part_index`) 청크 / (b) 참조 관계: 조항 인용 ↔ 피인용(`ref_chunk_seqs`) / (c) reverse-QA: LLM 질문 ↔ 원천 청크 | 사람 라벨 없이 구조에서 추출 |
| **hard negative** | BM25/Dense top-k 중 positive 제외 고순위 오답 + **인접-조항 negative(제42조 vs 제43조)** | 아래 참조 |

**핵심 연결**: critic이 이미 잡는 `generation_error`(인접 조항 착각) 실패모드가 **가장 좋은 hard-negative 소스**다. 즉 기존 trace/critic 데이터가 **병목 진단(측정)** 이자 **학습 신호(hard negative)** — 측정-먼저 철학과 자연스럽게 맞물린다.

### 2.4 운영 비용·리스크

- **재임베딩 + Qdrant 컬렉션 재생성 필수**. 무중단을 위해 **신규 컬렉션 병렬 구축 → A/B → 스위치** (기존 컬렉션 유지한 채 비교).
- 마이닝 쌍 분포에 **overfit** 위험 → 채택 판정은 반드시 held-out **실제 질문**으로만.

**구현 위치(예정)**: `scripts/mine_pairs.py` · `scripts/train_embedder.py` · `config/settings.py`(`EMBED_MODEL_PATH`) · `embed.py`/`qdrant.py` 차원·컬렉션.

---

## 3. Phase 2 — Qwen3 LoRA 도메인 어댑터 (조건부: generation-bound)

### 3.1 트리거

Phase 0에서 **generation-bound** 판정 — 검색 근거는 충분한데 Faithfulness/Answer Relevancy 낮고, critic `generation_error` 비율 높은데 hint-guided regenerate 개선률 낮음(현재 14.3%). 즉 검색이 아니라 **생성이 병목**.

### 3.2 방법

- **LoRA/QLoRA SFT**, `(retrieved context, query) → (grounded answer)`. rank 낮게, 형식(IRAC·조항 인용)과 근거 준수 강화.
- **서빙**: vLLM LoRA(`--enable-lora`, `--lora-modules`)로 어댑터 hot-load.
  - ⚠️ **검증 게이트**: **AWQ 양자화 베이스 + LoRA 서빙 호환성 실측 필요**. LoRA 학습은 보통 half-precision 베이스에서 하므로, AWQ 베이스에 어댑터를 얹는 서빙 경로가 실제로 되는지(또는 merge 후 재양자화가 필요한지)를 먼저 확인.
- **Feature flag**: `LLM_ADAPTER` env로 즉시 on/off — `CRITIC_DISPATCH_ENABLED`/`FEEDBACK_ENABLED` 와 동일 패턴, 코드 변경 없이 롤백.

### 3.3 데이터 — 코퍼스 마이닝 + rejection sampling

- **SFT 타깃**: 자기 파이프라인의 고품질 답변(groundedness/Faithfulness ≥ 임계)만 **rejection sampling**(self-distillation) → 전부 실제 retrieved context에 grounded.
- ⚠️ **echo-chamber 리스크**: 자기 출력 학습은 기존 오류를 증폭할 수 있음. 완화 = 고임계 필터 + 사람 spot-check subset + 필요 시 외부 teacher(GPT-4o)로 소량 보정.

### 3.4 리스크

- **catastrophic forgetting**(일반 질의 저하) → 일반셋 회귀 평가 필수.
- self-distillation 편향 · AWQ+LoRA 서빙 미검증(§3.2).

**구현 위치(예정)**: `scripts/build_sft.py` · `scripts/train_lora.py` · `clients.py`(LLM 어댑터 라우팅) · `config`(`LLM_ADAPTER`).

---

## 4. 채택 게이트 (공통) — "검증된 것만 메인 경로에"

파인튜닝 산출물은 **셋 다 충족하기 전까지** sidecar/실험 경로에만 둔다 (README 설계 원칙과 동일):

1. **held-out A/B에서 유의미 개선** (동일 judge·동일 입력)
2. **일반 쿼리 회귀 없음**
3. **즉시 롤백 가능** (임베딩=신규 컬렉션 스위치백 / LLM=`LLM_ADAPTER` 플래그 off)

precision/품질 임계는 README "검증되지 않은 영역"의 `semantic_judge` 도입 조건과 같은 정신 — 무리하게 메인 경로에 넣으면 신뢰도가 오히려 하락.

---

## 5. 범위 밖 / 인접 확장

- **`semantic_judge` 슬롯(NLI/HHEM)** 은 별개 축(검증기)이며 파인튜닝과 독립. 의미 반전 감지는 README *"검증되지 않은 영역"* 참조.
- **개인화·실시간 피처(Feast/Kafka)·멀티도메인 라우터** 는 이 로드맵 밖 (README *"의도적 미구현"*).
- 본 문서는 **모델 적응(대조학습·LoRA)과 그 게이트인 측정 확장**에 한정.

---

## 부록 — 설계 근거

- 대조학습: BGE-M3 / FlagEmbedding contrastive(InfoNCE + in-batch/hard negatives), hard-negative mining.
- 리랭커: cross-encoder 파인튜닝의 precision ROI.
- LoRA: Hu et al. 2021 · QLoRA: Dettmers et al. 2023 · vLLM multi-LoRA serving.
- 평가: RAGAS metric 정의(Faithfulness / Answer Relevancy / Context Precision / Context Recall) · Recall@k·MRR·nDCG · judge 분리(Zheng et al. NeurIPS 2023).
- 자기학습 주의: rejection sampling / self-distillation echo-chamber.

---
## 변경이력
<!-- 로드맵 갱신 시 여기에 append (oldest first) -->
- 2026-07-23 · 최초 작성 — 측정-먼저 3-Phase 로드맵(대조학습·LoRA·RAGAS 확장) 설계. 트리거 조건부 + 코퍼스 마이닝 데이터 전제.
- 2026-08-13 · Phase 0 판별식 구현(`diagnose_bottleneck.py`, `make diagnose`) — retrieval·ragas baseline을 읽어 retrieval-bound/generation-bound 판정 + 입력 품질 자가 채점. 현재 `generation-leaning(잠정)`: retrieval 충분(recall@5=1.0)·검색 병목 배제, 그러나 RAGAS faithfulness=nan+self-judge라 게이트 BLOCKED → 비편향 judge 재측정이 트리거. "병목 분해=없음" 해소.
- 2026-08-13 · retrieval 세그먼트 분해(`eval_retrieval --segment`, 도메인 어휘 vs 일반) — 도메인 질의가 recall@1 0.842·MRR 0.921로 일반(0.500·0.672)보다 우위. Phase 1(도메인 임베딩)의 전제(도메인 열위)를 **반증** → 트리거 더 멀어짐. 판별식이 @5(포화) 대신 @1·MRR로 세그먼트 비교하도록 정밀화.

---

## 부록 — 복합약관 파서 확장 (KB 계열, 2026-08-18 진단)

**문제**: `parse_clauses`/`split_sections`가 New치아·다이렉트 복합약관용으로 설계돼, KB 계열
복합약관(골든라이프·종합건강·자녀보험 700~1200p)에서 741p→**7조만** 파싱(원문 제N조 3,139회).
검색(청크)은 무관하게 되나, **관계형 조 파싱·parse 골든·SQL 경로가 새 회사에 안 열림.**

**근본 원인 (4중 breakage, 실측)**:
1. `RE_APPENDIX_START`가 본문 중간 인라인 `【별표N】` 참조(102개)를 부록 시작으로 오인 → `doc_end`가
   127K로 잘림(문서 748K). KB는 별표가 특약마다 흩어짐(`###### 별표N` 다수), 끝 1곳 아님.
2. 특약 경계가 `제N절`(3개) 아니라 **`제N장 ...특별약관`**(26개) — `RE_SUBPRODUCT`는 잡으나 TOC와 섞임.
3. `split_sections`가 위 doc_end 조기 잘림으로 **0개 서브약관** 반환.
4. 특약마다 조 번호 재시작 → 단조증가 가드가 끊고, `clause_id` 네임스페이스 충돌.

**착수 시 경로(제안)**: KB 계열 전용 분기 — TOC 끝(점선 리더 종료) 탐지 → `제N장/특별약관` 헤딩으로
본문 분할 → 특약별 `parse_clauses(region=)` (반각) → 별표 헤딩은 섹션 내부로 스킵 → 서브상품
네임스페이스(`{doc}_{특약}_제N조`, insurance_bge_m3_1024 컬렉션 방식). 무회귀는 기존 4문서
parse 골든(50)으로 보증. **포맷 특화라 골든 우선 확장 후 파서 수정 권장.**

**추가 진단(2026-08-18, 바운드 시도 결과)**: KB 골든라이프 실측상 더 험함 — ①TOC/색인이 문서
대부분(22,780/22,801줄) 차지 ②본문 조에 **인용 법령**(금융소비자보호법 제46조·제2조 등)이 섞여
`RE_EXTERNAL` 마스킹만으론 부족 ③특약 헤딩(제3~8장)이 본문보다 앞선 색인 영역에 위치 ④ODL 읽기순서
얽힘. → 한 번 시도로 안 되는 실제 프로젝트 확정. **전용 KB 파싱 경로 + 인용법령 필터 강화 + ODL
읽기순서 검증**이 선결. 검색(청크)은 무관하게 됨(recall 스코어보드로 확인) → 서비스 영향 없음.

**심층 진단(2026-08-20, 복합파서 인프라 완성 후) — KB 전용 프로파일 스펙**: 이 세션에 조-리셋 기반
복합파서(`detect_subcontracts`·`parse_compound`·`ingest_compound.sections_for_ingest`)가 완성돼
**회사미상 2건은 실 DB 적재 완료**(product 41→92). 그 인프라로 KB를 파고든 결과, KB는 **회사미상보다
한 계층 더 깊은 3층 구조**(문서 > 제N장 특약그룹 ×7 > 개별 특약 > 조)라 아래 5개가 KB 전용 프로파일로 선결:
1. **보통약관 조 중복(dedup)** — 골든라이프 보통약관 영역에 제1~53조가 **전부 존재하나 101매치/53고유 = ~2× 중복**(ODL이 요약+본문 or 다단 중복 방출). 단조 파스가 첫 39조만 잡음. → 조 번호별 dedup(본문 실한 것 우선)으로 53조 복원 가능.
2. **요약서/가이드북 배제** — 문서 앞부분(line 1~13000)이 목차·약관이용가이드북·상품개요·**주요내용 요약서**(`###### ■ 계약의 소멸`·`■ 보험금의 지급절차` 등 조-유사 내용)라 조 오탐 다수. 실 특약 body는 line 13000~19000(제6~9장). → 실 약관 body 영역 탐지(요약서 마커 제외)가 선결.
3. **제N장 헤딩 3중 중복** — 각 제N장이 TOC + `#### 제N장…특별약관` + `###### 제N장…특별약관`으로 3번 등장(ODL 중복). 특약그룹 경계 인식 시 dedup 필요.
4. **개별 특약 헤딩 = 코드명**(끝이 '특별약관' 아님, 예 `###### 경증이상치매진단비`) — `sections_for_ingest`의 `endswith('특별약관')` 필터가 KB엔 안 맞아 특약 3개만 통과(회사미상은 40개 통과). → 제N장 특약그룹 **내부**의 코드명 헤딩을 특약으로 인식하는 KB 규칙 필요.
5. **헤딩 오귀속** — 조-리셋 런의 직전 헤딩이 이전 특약의 준용규정 본문("…이 특별약관에서 정하지…")을 잡음. → 헤딩 부착을 `#`-헤딩 라인 우선으로.
**결론(잠정, 아래 근본원인 확정으로 정정됨)**: KB는 제3 프로파일(kb)로 요약서 배제+보통약관 dedup+제N장 분해가 필요.

**⚑ 근본 원인 확정(2026-08-20, 재추출이 선결 — 파서 아님)**: 위 5개를 파고든 끝에 **진짜 블로커는 ODL 조 중복 방출**임을 증명:
- **모든 조가 ~2× 중복** — 보통약관 제1~53조가 101매치/53고유(각 2×), 특약도 한 구간 조매치 `[1,2,3,4,5,6,7,8,1,2,3,4,5,6]`(제1~8 뒤 제1~6 재등장, 중복 {1:2…6:2}). 자녀보험 661런의 대부분이 이 **중복이 만든 false-reset**.
- **인용법령 아님(반증)** — 금융소비자보호법·"보통약관 제N조" 등 확장 마스킹 = **0 효과**(661→661). 마스킹으로 안 줄어듦이 중복이 원인임을 확정.
- **요약서 배제만으론 미미** — kb_body_region([보통약관 제1조 목적 → 트레일링 인용법규정])으로 front-matter 잘라도 143→126·661→641(대부분 body 내부 중복).
→ **KB는 파서 프로파일이 아니라 추출(ODL) 문제**. 중복된 입력엔 어떤 파서도 안 통함. 클린 dedup도 어려움(요약본+전문이 조 수가 달라 균일 2× 아님 + 특약끼리 제1조 공유라 전역 dedup 불가 → 특약 세그먼트 후 dedup인데 세그먼트가 중복 때문에 깨짐 = 순환). **선결 = ODL 재추출**(KB PDF의 다단/요약 레이아웃을 조 중복 없이 뽑는 설정) 또는 요약본 영역 판별 후 전문만 채택. 검색(청크)은 무관하게 됨 → 서비스 영향 없음(관계형 SQL 경로만 KB 특약 미개통). **파서 profile 착수 전에 재추출로 중복 제거가 순서.** (회사미상은 중복 없어 복합파서로 이미 적재 완료 — KB만의 병리.)
