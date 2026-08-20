# 작업 현황 (working status)

> 이 문서는 **지금 어디까지 됐고 · 다음 후보가 뭐고 · 어디로 접속하나**를 한 장에 모은 작업 대시보드다.
> (포트폴리오 소개는 [portfolio.html](portfolio.html), 에이전트 지침은 [CLAUDE.md](../CLAUDE.md),
> 파인튜닝 로드맵은 [roadmap.md](roadmap.md) — 역할이 다르다.)

## 완성된 계층

| 계층 | 구현 | 게이트/검증 |
|---|---|---|
| **결정론 3경로 라우터** | `/answer` 자동분기 → payout·terms·coverage·exclusion (SQL, LLM 미사용) | `make eval-sql-routing` 16문항 accuracy 1.0 |
| **RAG 서빙** | Dense(BGE-M3)+BM25+RRF+CrossEncoder rerank+CRAG+Self-RAG | `make eval-retrieval` recall@5=1.0·MRR=0.86 |
| **관계형 추출** | product·clause(회사미상 특약 포함 92·628)·payout_rule·coverage_range·annex_row (실 DB) | `make check` 골든 10종 green(gate 복합약관 인지) |
| **파싱** | 조 파서 + 항/호/목 세분(`parse_subitems`) + **복합약관 분해**(`parse_compound` 조-리셋) + CLI 정밀 뷰 | 파싱골든 **50/50** + 유닛(structure·subitems·subcontracts) |
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
- **🔧 복합약관 다절 파서 — 파싱·배선 완료, DB 적용만 스택 대기** — `parse_clauses` 메인경로는 복합약관을 첫 서브계약(보통약관)만 파싱(단조 break). KB(특약 다절)·회사미상 2건 모두 복합약관(상해질병 216× 특별약관 `## 특별약관` 섹션·수술비=LIG파워업연금·KB 제N장). **조-리셋 불변식**(특약은 각각 제1조부터 재시작)으로 3계층 구현(회귀 0):
    1. `detect_subcontracts`(감지) + `parse_compound`(개별 파싱) — 메인경로 미변경 사이드카. 실측 상해질병 **42조 → 42서브계약 231조**(특약 제목 정확)·KB 자녀보험 특약 540개 감지.
    2. `check_parsing` 정밀 진단("과소파싱 7조"→"복합약관: 보통약관 39조 + 특약 99개") + `--subs` 특약 분해 뷰.
    3. `ingest_compound.sections_for_ingest` 폴백 배선 — split_sections(제N절)≥2면 기존(New치아·다이렉트 **동일 반환 검증**=회귀 0), 없으면 parse_compound. 유닛 8.
    - **dry-run 전수 검증(6 복합약관, 스택 없이 SQL 생성)이 준비 상태를 정밀 분리**:
      - ✅ **회사미상 ×2 DB 적재 완료(2026-08-20)** — `ingest_compound`으로 실 DB 적재: **product 41→92**(보통약관 2 + 특약 **49개** parent 연결), **clause 307→628**(+321). 특약 조 제목까지 쿼리 가능(암진단비 특약 7조: 계약무효·소멸·부활·지급사유·세부규정·암정의·준용). 적재 중 **annex PK 충돌 버그 수정**(복합약관 별표1 ×11 반복 → load_annexes 중복 annex_id 제거, 단일문서 no-op). parse_compound가 raw→DB end-to-end 작동 실증.
      - ⛔ **KB ×4는 적재 금지(과소포착)** — dry-run이 검출: KB 특약 헤딩이 '특별약관'으로 안 끝나고(제N장 구조 + 【갱신계약】 태그), 조-리셋이 과분할(자녀보험 661런 중 540이 준용규정 본문 오탐)이라 `endswith` 필터가 precision-first로 막음(자녀보험 특약 3개뿐·거대 미분할 보통약관). **KB는 제N장 인식 + false-reset 필터가 별도 필요**(roadmap 복합파서 KB절, deep).
    - **⚠️ 적재 후 측정이 다음 갭 검출(2026-08-20, 회사미상 SQL 경로 확장 시도)**: `extract_terms`가 회사미상 청약철회를 **오추출**(수술비 실제 "15일 이내 청약을 철회"인데 3일 반환기일/3개월 취소를 뭄). 원인=정규식이 **큰 복합문서 전체를 훑어 첫 '철회…N일' 매치**를 잡음(line 34-35) — 특약마다 철회 언급 많은 복합약관에서 보통약관 진짜값을 놓침. **측정이 틀린 적재를 차단**(precision-first): 검증 없이 넣었으면 오답 데이터. → **회사미상 terms/payout 적재는 extract_terms를 청약철회 조-스코프로 정밀화한 뒤**(기존 3사 골든 8/8 무회귀 보장 필요=별도 로버스트니스 작업). 상해질병 15일은 그럴듯하나 이 역시 조-대조 검증 후 적재.
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
