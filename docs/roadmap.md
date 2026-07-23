# 모델 고도화 로드맵 (측정 → 조건부 파인튜닝)

> 이 문서는 **아직 구현되지 않은** 확장 방향의 설계·근거·트리거 조건이다. 실제 학습은 GPU 서버에서 수행하며, README의 *"확장 지점"* · *"의도적 미구현"* 과 같은 원칙 — **측정된 이득만 메인 경로에** — 을 따른다. 트리거 조건이 충족되기 전에는 도입하지 않는 게 기본값(default = 미도입).

세 축을 다룬다: **① BGE-M3 대조학습 임베딩 파인튜닝 · ② Qwen3-14B LoRA 도메인 어댑터 · ③ RAGAS/Retrieval 평가 확장**. 다만 **순서가 아니라 트리거 게이트**다 — ③(측정)이 선행 필수이고, 그 측정 결과가 ①이냐 ②냐를 가른다.

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
| Retrieval | 보류 (Context Utilization proxy) | **Recall@k · MRR · nDCG@k** | 검색 품질 절대치. 파인튜닝 전/후 비교 기준선 |
| 병목 분해 | 없음 | **retrieval-bound / generation-bound 판별식** | Context Recall 낮음 → retrieval / Context Recall 높은데 Faithfulness 낮음 → generation |

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

**구현 위치(예정)**: `scripts/eval_ragas.py` 지표 확장 · 신규 `scripts/eval_retrieval.py` · 신규 `scripts/build_eval_set.py`(reverse-QA 마이닝).

---

## 2. Phase 1 — BGE-M3 대조학습 임베딩 파인튜닝 (조건부: retrieval-bound)

### 2.1 트리거

Phase 0에서 **retrieval-bound** 판정 + 도메인 용어 쿼리가 일반 쿼리보다 Recall 유의미하게 낮음. (트리거 미충족 시 미도입.)

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

## 3. Phase 2 — Qwen3-14B LoRA 도메인 어댑터 (조건부: generation-bound)

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
