# 작업 현황 (working status)

> 이 문서는 **지금 어디까지 됐고 · 다음 후보가 뭐고 · 어디로 접속하나**를 한 장에 모은 작업 대시보드다.
> (포트폴리오 소개는 [portfolio.html](portfolio.html), 에이전트 지침은 [CLAUDE.md](../CLAUDE.md),
> 파인튜닝 로드맵은 [roadmap.md](roadmap.md) — 역할이 다르다.)

## 완성된 계층

| 계층 | 구현 | 게이트/검증 |
|---|---|---|
| **결정론 3경로 라우터** | `/answer` 자동분기 → payout·terms·coverage·exclusion (SQL, LLM 미사용) | `make eval-sql-routing` 16문항 accuracy 1.0 |
| **RAG 서빙** | Dense(BGE-M3)+BM25+RRF+CrossEncoder rerank+CRAG+Self-RAG | `make eval-retrieval` recall@5=1.0·MRR=0.86 |
| **관계형 추출** | product·clause·payout_rule·coverage_range·annex_row·clause_ref (실 DB) | `make check` 골든 10종 green |
| **파싱** | 조 파서 + 항/호/목 세분(`parse_subitems`) | 파싱골든 **50/50** + 유닛(structure·subitems) |
| **측정 계층** | 지연·부하 벤치 / 병목 판별식 / 검색 세그먼트 | `make bench`·`bench-load`·`diagnose` |
| **관측** | trace 12섹션 + feedback + input/output guard | `make trace`·`smoke` |
| **포트폴리오** | 라이브 [deokjinlog.github.io/docs-rag](https://deokjinlog.github.io/docs-rag/) | GitHub Pages |

## 측정 스냅샷 (실측, 2026-08-13~14)

- 검색 recall@5=1.00 · @3=0.96 · @1=0.76 · MRR=0.86 (25문항·5문서). 도메인 어휘가 일반보다 우위(recall@1 0.84 vs 0.50).
- SQL 경로 지연 p50 ~6ms(GPU 무관) · 부하 피크 ~183 QPS(에러 0).
- 병목 판정 = `generation-leaning(잠정, BLOCKED)` — retrieval 배제, 생성측은 비편향 judge RAGAS 재측정 필요(GPU+키).
- 유닛 199 + 통합 21 · 파싱골든 50 · 결정론골든 10종.

## 코퍼스 상태

- **색인 완료 11문서** → **13,080청크**(docs_rag_v1, 758에서 17배). **5개 회사**: 라이나(4)·New치아(AXA)·다이렉트·**KB**(골든라이프 1924·종합건강 2294·자녀보험 7198)·**회사미상**(상해질병 410·수술비 325). 2026-08-18 대량 확장(GPU embed).
- **회사 넘어 검색 검증**: R14(새 회사) rerank 0.992·전코퍼스 rank 3. **17배 코퍼스 recall 재측정 = recall@5=1.0·MRR=0.861 무회귀**(baseline 동일) — cross-doc 오염 0, 기존 정답 안 밀려남. (rerank 점수는 경쟁 심화로 하락하나 순위 유지.)
### 골든 확장 (진행 중, 2026-08-18) — "코퍼스 넓힘 ≠ 골든 넓힘"
코퍼스는 17배 넓혔지만 **골든(시험지)은 아직 옛 5문서만**(retrieval 25·parse 50). 새 회사 품질을 측정으로 보려면 골든을 넓혀야 함. 진행:
- **`scripts/ask.py`**(`uv run python scripts/ask.py "질문"`) — 가벼운 검증 CLI. 새 회사 검색 실측: KB 자녀보험 청크 rerank 0.962.
- **retrieval 골든 초안** `golden_retrieval_newcorpus.jsonl`(6문항, KB 치매·장기요양·회사미상 상해 등, 전 코퍼스 검색). `eval_retrieval --golden <파일>`로 채점(baseline 무관).
- **⚠️ 조 파싱은 KB 복합약관에서 깨짐** — `parse_clauses`가 복합약관을 첫 섹션(sections[0]→[1])만 파싱해 KB(특약 다절)는 741p→7조만 잡음(원문 제N조 3,139회). 회사미상은 조 gap. 검색(청크)은 무관하게 됨. **parse 골든 확장·SQL 경로는 복합약관 다절 파서 fix 후**(별도 작업).
- **색인 대기**: KB_플러스운전자(1202p)는 세션 중 파일명 인코딩 꼬임 → 클린 재복사 필요. 추출·embed fix는 검증됨.

### 색인 파이프라인 수정 (2026-08-18, 회사 넘어 확장 중 발견)
- **ODL Java 힙 OOM**(700~1200p 대형 약관 추출 실패) → `-Xmx2g`(odl compose env, 3g캡 내). DRM/포맷 아님.
- **CPU 임베딩 병목**(1200p→~2900청크 ~90분) → **GPU embed 토글**(`make ingest-gpu`, ingest 모드 GPU 놀 때). 96p 문서 ~60초 완주로 검증.
- 관찰: 회사미상 문서 heading_path 약함 → 새 포맷 파싱 품질은 후속 진단 대상.

## 다음 후보 (우선순위 아님, 골라 쓰기)

| # | 할 일 | 성격 | 필요 자원 |
|---|---|---|---|
| C1 | **코퍼스 확장** — 대기 PDF 색인 → 파서 회사-넘어 일반화 실측 | 데이터·로버스트 | ingest 스택(paddle·odl, CPU OCR) |
| C2 | `parse_subitems` → `clause.hang` **배선** (현재 307/0 빈칸) | 정밀 인용 | 없음(자립) |
| C3 | **CI 자동 게이트** — push마다 유닛+판별식 | 품질 | GitHub Actions |
| C4 | **generation-bound 확정** — 비편향 judge RAGAS | 측정 | GPU(vLLM)+OPENAI_API_KEY |
| C5 | 골든 2인 라벨 / 확장 (roadmap A3) | 신뢰도 | 도메인 판단 |

## 접속 레퍼런스

| | |
|---|---|
| API 테스트 (Swagger) | http://localhost:8002/docs |
| Qdrant 벡터 탐색 | http://localhost:6333/dashboard |
| Celery / RabbitMQ | http://localhost:5555 / http://localhost:15672 |
| PostgreSQL | `docker compose exec postgres psql -U docsrag -d docsrag` · 외부 localhost:5433 (docsrag/docsrag2026) |
| 파일 | raw `data/output/raw/{문서}.json·.md` · processed `data/output/processed/{문서}/clauses.jsonl` · 골든 `data/eval/golden_*.jsonl` |
| 스택 모드 | `make lite`(검색+SQL, GPU프리) · `make ingest`(색인, paddle·odl) · `make answer`(생성, vLLM) · `make recover`(WSL깨짐 복구) |
