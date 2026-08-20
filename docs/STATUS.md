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
| **파싱** | 조 파서 + 항/호/목 세분(`parse_subitems`) + **CLI 정밀 뷰**(항①→호1.→목가.) | 파싱골든 **50/50** + 유닛(structure·subitems) |
| **측정 계층** | 지연·부하 벤치 / 병목 판별식 / 검색 세그먼트 | `make bench`·`bench-load`·`diagnose` |
| **관측** | trace 12섹션 + feedback + input/output guard | `make trace`·`smoke` |
| **CI 자동 게이트** | GitHub Actions — push마다 194 유닛 + 병목 판별식(스택 없이) | ✅ green (~30s, paddle/torch 없이 light dep) |
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
- **CLI 도구**(`uv run`) — `scripts/ask.py "질문"`(라우팅·답·근거) · `scripts/check_parsing.py`(전 약관 조 파싱 품질: 6정상/4 KB 과소파싱/2 회사미상 gap, 밀도 K/조로 검출) · `check_parsing.py <문서> <조번호>`(**조의 항/호/목 정밀 뷰** — "제5조 3항 2호" 정밀 인용의 실물, `parse_subitems`를 실제 약관에서 눈으로 소비).
- **회사별 recall 스코어보드** — 새 문서 6개 10문항(치매·장기요양·태아·영구치·LIG·배상책임 등). **핵심 발견: 새 회사 검색은 실제로 훌륭함** — LIG·배상책임 질의도 정답 문서(R15·R18)를 **top-1~5 rerank 0.98~0.99**로 찾았음. 초기 "미검출"은 검색 실패가 아니라 **내 앵커가 답 청크에 없던 false negative**(앵커=상품명/헤딩, 답 청크엔 실제 특약 내용). → 앵커를 답 청크 verbatim으로 교정 후 **recall@5=0.900·recall@10=1.000·MRR=0.867**(8/10 rank1). 새 회사 6문서 전부 모든 답 검색됨. **교훈(중요): retrieval 골든 앵커는 doc-유니크가 아니라 '질의가 가져오는 답 청크의 verbatim'이어야 함** — eval_retrieval 방법론(원문 앵커)의 실전 함정.
- **retrieval 골든 초안** `golden_retrieval_newcorpus.jsonl`(6문항, KB 치매·장기요양·회사미상 등, 전 코퍼스 검색). `eval_retrieval --golden <파일>`로 채점(baseline 무관). **recall@5: 0.667 → 0.833**(앵커 1개 교정 후, 5/6 rank 1 · MRR 0.861 · recall@10=1.0). 루프 실증: 1차 실패 2건이 검색 gap 아니라 **내 앵커 불량**("급격하고도 우연한"=7개 문서 보일러플레이트)임을 진단 → R14 유니크(변호사선임비용)로 교정 → rank 1. **교훈: 겹치는 표준 문구 말고 각 문서 유니크 급부로 앵커.** 남은 rank6(특정부위 부담보)는 R14 유니크나 경쟁서 6위(recall@10=1.0). 확정 골든은 전문가 2인 라벨(A3).
- **⚠️ 조 파싱은 복합약관에서 깨짐** — `parse_clauses`가 복합약관을 첫 섹션(sections[0]→[1])만 파싱해 KB(특약 다절)는 741p→7조만 잡음(원문 제N조 3,139회). **회사미상 2건도 복합약관으로 판명**(상해질병 216× 특별약관 `## 보통약관`/`## 특별약관` 섹션 · 수술비=LIG파워업연금 보통+특약 수십). 검색(청크)은 무관하게 됨. **parse 골든 확장·SQL 경로는 복합약관 다절 파서 fix 후**(별도 작업).
- **✅ 파서 로버스트니스 개선(자립)** — 진단 중 별개 버그 수정: 반각 프로파일이 **본문을 다음 줄에 두는 마크다운 헤딩 조**(`## 제3조(계약의 무효)`·`###### 제1조`)를 목차로 오인해 통째 누락하던 것을, `#`-헤딩 마커 면제로 복원(목차·인라인참조엔 `#` 없어 precision 유지). 회사미상 상해질병 **38→42조**(제3·4·16 복원)·수술비 제1~6 복원. 클린 문서(라이나·New치아·다이렉트) 무회귀. 유닛 5(`test_parse_heading_profile`: 복원+목차배제 동시 잠금). 남은 gap(불릿헤딩 `- 제N조` + 복합구조 단조break)은 복합파서 소관.
- **색인 대기**: KB_플러스운전자(1202p)는 세션 중 파일명 인코딩 꼬임 → 클린 재복사 필요. 추출·embed fix는 검증됨.

### 색인 파이프라인 수정 (2026-08-18, 회사 넘어 확장 중 발견)
- **ODL Java 힙 OOM**(700~1200p 대형 약관 추출 실패) → `-Xmx2g`(odl compose env, 3g캡 내). DRM/포맷 아님.
- **CPU 임베딩 병목**(1200p→~2900청크 ~90분) → **GPU embed 토글**(`make ingest-gpu`, ingest 모드 GPU 놀 때). 96p 문서 ~60초 완주로 검증.
- 관찰: 회사미상 문서 heading_path 약함 → **후속 진단 완료**(둘 다 복합약관 + 반각 마크다운헤딩 조 누락 버그, 위 파서 로버스트니스 항목에서 `#`-헤딩 복원).

## 다음 후보 (우선순위 아님, 골라 쓰기)

| # | 할 일 | 성격 | 필요 자원 |
|---|---|---|---|
| C1 | **코퍼스 확장** — 대기 PDF 색인 → 파서 회사-넘어 일반화 실측 | 데이터·로버스트 | ingest 스택(paddle·odl, CPU OCR) |
| ~~C2~~ | ~~`parse_subitems` → clause 배선~~ ✅ **CLI 소비자로 완성**(항/호/목 정밀 뷰). DB 컬럼(clause.items)은 서빙 소비자 생길 때까지 보류(죽은 인프라 회피) | 정밀 인용 | 없음(자립) |
| ~~C3~~ | ~~CI 자동 게이트~~ ✅ **완료**(194 유닛+판별식 green) | 품질 | GitHub Actions |
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
