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

- **색인 완료 5문서** → 758청크(docs_rag_v1) + 조단위 326(insurance_bge_m3_1024). 3회사(라이나·New치아·다이렉트).
- **색인 대기 ~6 PDF** (`data/input/`): KB 운전자상해·간편건강·골든라이프·자녀보험·종합건강 · 라이나 실버치아 · 손보미상 상해질병/수술비. → **회사 넘어 일반화 테스트용**.

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
