# docs-rag — host에서 직접 실행하는 dev 서버 + 평가·테스트 명령.
# (docker / uv / 기본 git 명령은 표준이라 별도 alias 두지 않음.)

include .env
export

.PHONY: api celery flower \
        test test-host test-integration test-rag test-guards \
        eval eval-retrieval chunk-quality eval-routing eval-sql-routing feedback-submit trace trace-feedback smoke eval-ocr eval-index bench bench-load diagnose \
        mem watch recover lite ingest ingest-gpu retrieve-gpu answer full


# ─── Local Dev (host에서 직접 띄울 때, docker 미사용) ─────────────────────

api: ## uvicorn 직접 실행 (--reload, port 8002)
	uv run uvicorn api:app --host 0.0.0.0 --port 8002 --reload --app-dir src

celery: ## Celery worker 직접 실행 (threads pool, concurrency=4)
	cd src && uv run celery -A celery_app:celery_app worker --loglevel=info --pool=threads --concurrency=4 -E

flower: ## Flower 모니터링 UI (port 5555)
	cd src && uv run celery -A celery_app:celery_app flower --port=5555


# ─── Tests ────────────────────────────────────────────────────────────────

test: test-host ## 기본 = host 단위 테스트

test-host: ## 단위 테스트 (host, integration mark 자동 skip)
	uv run pytest tests/ -v

test-integration: ## E2E 테스트 (docker exec, 모델 파일 의존)
	docker compose exec api uv run pytest tests/ -v -o "addopts=" -m integration

test-rag: ## tests/rag/ 만
	uv run pytest tests/rag/ -v

test-guards: ## tests/guards/ 만
	uv run pytest tests/guards/ -v

check: ## 관계형 추출 자립 골든 9종 + 전처리 게이트 (배포 관문, 스택 불필요, 회귀 시 exit 1)
	python3 scripts/check.py

eval-semantic-route: ## 시맨틱 라우터 의도 분류 채점 (임베딩 모델 필요, API·DB 불필요)
	docker compose exec -T api python scripts/eval_semantic_route.py

eval-semantic-holdout: ## 시맨틱 라우터 held-out 회귀 게이트 (baseline 오라우팅률 대조)
	docker compose exec -T api python scripts/eval_semantic_route.py --golden data/eval/golden_routing_holdout.jsonl

eval-tool-route: ## 툴콜링 라우팅 채점 (vLLM 에 --enable-auto-tool-choice 필요)
	python3 scripts/eval_tool_routing.py

eval-multihop: ## 멀티홉(에이전트) 골든 — 툴 recall·금지툴·요소 recall (스택 + vLLM 필요)
	python3 scripts/eval_multihop.py

compare-parsers: ## 파서 비교 (ODL vs PaddleOCR-VL) — MD_A/MD_B 로 마크다운 두 개를 준다
	python3 scripts/compare_parsers.py --md-a "$(MD_A)" --md-b "$(MD_B)"

triage: ## 페이지 분류 분포 (캐스케이드 M1) — 어느 경로로 갈 페이지가 몇 %인가
	uv run --no-project --with pymupdf python scripts/triage_pages.py --all

scanset: ## 합성 스캔셋 생성 (캐스케이드 M3) — 네이티브 PDF 를 래스터라이즈, 정답은 텍스트 레이어
	uv run --no-project --with pymupdf --with pillow python scripts/make_scanset.py --pdf "$(PDF)" --out data/eval/scanset

eval-scanset: ## 스캔셋 채점 (OCR vs VL). ENGINE=ocr|vl 로 실행, 없으면 저장된 결과 리포트
	uv run --no-project --with rapidfuzz python scripts/eval_scanset.py $(if $(ENGINE),--engine $(ENGINE),)

up: ## 스택 기동 + 검증 (마운트·응답 대조). WSL 재부팅 후엔 compose up 대신 이걸 쓴다
	bash scripts/stack_up.sh


# ─── Eval & Observability ─────────────────────────────────────────────────

eval: ## RAGAS Triad 평가 (Judge=GPT-4o-mini 권장 — OPENAI_API_KEY env 필요. --basic 플래그는 직접 호출)
	uv run python scripts/eval_ragas.py

eval-retrieval: ## 검색 골든셋 recall@k · MRR (스택 필요 — /retrieve 호출. --update-baseline로 기준선 고정. --segment로 도메인/일반 분해)
	python3 scripts/eval_retrieval.py

chunk-quality: ## RAG 청크 전처리 완성도 게이트 (br·img·page·점선·고아heading·커버리지, 스택 불필요)
	python3 scripts/eval_chunk_quality.py

eval-routing: ## 라우팅 골든 — 5-type 분류기 정확도 (순수 정규식, 스택 불필요. --update-baseline)
	python3 scripts/eval_routing.py

eval-sql-routing: ## SQL 3경로 라우팅 골든 — payout/terms/coverage vs RAG (스택 필요, /answer 호출. --update-baseline)
	python3 scripts/eval_sql_routing.py

feedback-submit: ## (producer, eval_ragas.py --submit-feedback) RAGAS Faithfulness → signal 매핑·DB 적재
	uv run python scripts/eval_ragas.py --submit-feedback

trace: ## 서빙 trace 11-섹션 집계 (당일)
	uv run python scripts/trace_summary.py

trace-feedback: ## trace 집계 + Feedback DB 7일 JOIN (consumer)
	uv run python scripts/trace_summary.py --feedback --days 7

smoke: ## 관측 인프라 DoD 11-step 자동 검증
	uv run python scripts/smoke_test.py

eval-ocr: ## OCR 필터 통과율 + confidence 분포
	uv run python scripts/eval_ocr.py

eval-index: ## Qdrant 벡터 공간 헬스 (Dispersion + Confusion Rate)
	uv run python scripts/eval_index_health.py

bench: ## 서빙 지연 벤치 — SQL 경로(ms) vs retrieve floor. 모드 자동기록. data/bench/<날짜>/ 보존
	python3 scripts/bench.py --date $(shell date +%Y%m%d)

bench-load: ## 부하·동시성 스윕 — SQL 경로 QPS·부하 하 지연(p50/p95/p99). data/bench/<날짜>/load.json
	python3 scripts/bench.py --load --date $(shell date +%Y%m%d)

diagnose: ## 병목 분해 — retrieval-bound vs generation-bound 판별식(파인튜닝 게이트, 스택 불필요)
	python3 scripts/diagnose_bottleneck.py


# ─── Infra / Health (로컬 8GB·WSL 안정성) ─────────────────────────────────

mem: ## 컨테이너별 메모리 사용/상한 + WSL 스왑 (1회 스냅샷)
	@docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" | grep -E 'NAME|docs-rag'
	@echo "── WSL ──"; free -h | awk 'NR<=3'

watch: ## 메모리 실시간 모니터 (3초 갱신 + 경고). Ctrl+C로 종료
	@while true; do \
	  clear; \
	  a=$$(free -m | awk 'NR==2{print $$7}'); sw=$$(free -m | awk 'NR==3{print $$3}'); \
	  echo "═══ docs-rag 메모리 모니터  ($$(date +%H:%M:%S), Ctrl+C 종료) ═══"; \
	  free -h | awk 'NR<=3'; \
	  if [ $$a -lt 1500 ]; then echo "  🔴 위험: available $${a}MB (<1.5GB) — 곧 스왑/크래시. 세션 줄이거나 make lite"; \
	  elif [ $$sw -gt 8000 ]; then echo "  🟡 주의: swap $${sw}MB — 압박 높음(느려질 수 있음)"; \
	  else echo "  🟢 여유: available $${a}MB, swap $${sw}MB"; fi; \
	  echo "── docs-rag 컨테이너 ──"; \
	  docker stats --no-stream --format "  {{.Name}}  {{.MemUsage}}" 2>/dev/null | grep docs-rag; \
	  sleep 3; \
	done

recover: ## 스택 반쯤 깨졌을 때(WSL 재시작 여파: DNS·마운트 소실) 네트워크째 재생성
	docker compose down && docker compose up -d
	@echo "→ vLLM 재로드 1~2분 대기 후 /answer 가능 (make mem 으로 메모리 확인)"

# 모드별 메모리 발자국 (실측, 로컬 15GB WSL 기준) — 필요한 것만 켜서 다른 작업(Ralph)과 공존:
#   lite   ~2.0GB  검색/관계형/eval        (vllm·paddle·odl OFF)
#   ingest ~3.7GB  색인 파이프라인          (paddle·odl ON, vllm OFF — 파이프라인은 LLM 불필요)
#   answer ~4.5GB  /answer 답변생성         (vllm ON, paddle·odl OFF)
#   full   ~6.2GB  색인 + /answer 동시      (전부 ON — Ralph 공존 빠듯)

# ── compose 프로필 (D1) ──────────────────────────────────────────────────────
# 전 서비스를 함께 띄우면 mem_limit 합계가 WSL 15Gi 를 넘어 스택이 통째로 죽는다(실측 4회).
# 한 번에 뜨는 조합을 프로필로 못박고 `make mem-budget` 이 합계를 기계로 검증한다.
backup: ## DB 덤프 + Qdrant 스냅샷 → data/backup/. **파괴적 작업 전에 반드시**
	@mkdir -p data/backup
	@f=data/backup/docsrag_$$(date +%Y%m%d_%H%M).sql; \
	 docker compose exec -T postgres pg_dump -U docsrag -d docsrag --no-owner > $$f && \
	 echo "  DB 백업: $$f ($$(du -h $$f | cut -f1))"
	@# Qdrant 도 함께 — 2026-09-10 복구가 가능했던 건 **비싼 것이 DB 밖에 있어서**였다.
	@# 다음 사고에서 비싼 것이 어디 있을지는 모르니, 양쪽을 같이 뜬다.
	@# 스냅샷은 Qdrant 볼륨 안(snapshots/)에 남는다 — 컨테이너를 지워도 볼륨은 유지된다.
	@curl -sf -X POST localhost:6333/collections/docs_rag_v1/snapshots \
	  | python3 -c "import json,sys; d=json.load(sys.stdin).get('result') or {}; \
	    print('  Qdrant 스냅샷:', d.get('name','?'), f\"({d.get('size',0)/1e6:.0f}MB)\")" \
	  || echo "  ⚠ Qdrant 스냅샷 실패 (qdrant 미기동?) — DB 백업만 완료"

db-reset: backup ## ⚠ 문서 테이블 전체 삭제 후 재생성. backup 을 **의존성으로** 먼저 돈다
	@echo "  ⚠ tb_document_* 를 전부 지웁니다. 5초 후 진행 (Ctrl+C 로 중단)"
	@sleep 5
	cat db/schema_reset.sql | docker compose exec -T postgres psql -U docsrag -d docsrag
	cat db/schema.sql       | docker compose exec -T postgres psql -U docsrag -d docsrag
	@echo "  → 복구가 필요하면: docker compose exec -T api python /app/scripts/recover_from_artifacts.py --apply"

db-restore: ## 최신 백업으로 복원 (data/backup 의 가장 최근 .sql)
	@f=$$(ls -t data/backup/*.sql 2>/dev/null | head -1); \
	 [ -n "$$f" ] || { echo "  백업 없음"; exit 1; }; \
	 echo "  복원: $$f"; \
	 cat $$f | docker compose exec -T postgres psql -U docsrag -d docsrag >/dev/null && echo "  완료"

down: ## 전 프로필 정지·제거 — ⚠ 그냥 `docker compose down` 은 프로필 서비스를 안 내린다
	COMPOSE_PROFILES=serve,ingest,ocr,inspect docker compose down --remove-orphans

mem-budget: ## 프로필별 mem_limit 합계 검증 (11Gi 상한, 한도 미설정도 실패)
	python3 scripts/mem_budget.py
	python3 scripts/mem_budget.py -f docker-compose.yml -f docker-compose.paddle-gpu.yml

serve: ## 서빙 프로필 (9.0Gi) — api·vllm·infra. /answer·/retrieve
	PROFILE=serve bash scripts/stack_up.sh

ingest: ## 인제스트 프로필 (8.5Gi) — celery·odl·infra. extract→chunk→embed (OCR 제외)
	PROFILE=ingest bash scripts/stack_up.sh

ocr: ## OCR 프로필 (10.0Gi) — celery-ocr(동시성1)·paddle GPU·infra. ocr 큐만 소비
	docker compose -f docker-compose.yml -f docker-compose.paddle-gpu.yml --profile ocr up -d
	@echo "→ ocr 큐 소비 시작. 끝나면 'docker compose --profile ocr down' 으로 GPU 반납"

inspect: ## 관측 프로필 (5.0Gi) — api(Inspector·Eval Studio)·infra. 읽기 전용
	PROFILE=inspect bash scripts/stack_up.sh

lite: ## 경량 — 앱만 내리고 infra 유지 (make check·자립 골든용)
	docker compose stop vllm paddle odl celery celery-ocr flower 2>/dev/null || true
	@$(MAKE) --no-print-directory mem

ocr-gpu: ## 이미지 OCR을 GPU로 — 성능 옵션이 아니라 **동작 조건**(CPU 추론은 컨테이너가 죽어 image 청크가 0개였다)
	docker compose -f docker-compose.yml -f docker-compose.paddle-gpu.yml up -d --force-recreate paddle
	@# ⚠ 메모리 예산: 선언된 mem_limit 합계가 ~21g 인데 WSL 은 15Gi 다. paddle GPU 경로는
	@# 호스트 RAM 3.9GiB 를 쓴다(실측). 안 비우면 Docker 가 스택 전체를 SIGKILL 한다
	@# (실측 2026-09-09: 배치 도중 8개 컨테이너 동반 종료). 안 쓰는 무거운 것들을 내린다.
	docker compose stop vllm odl flower 2>/dev/null || true
	@echo "→ paddle GPU (vllm·odl·flower 중지). 끝나면 'make up'으로 전체 복귀"

ingest-gpu: ## 색인 가속 — 임베더를 GPU로(ingest 모드 GPU 놀 때 10~50배). 끝나면 `docker compose up -d celery`로 CPU 복귀
	docker compose -f docker-compose.yml -f docker-compose.ingest-gpu.yml up -d celery
	docker compose stop vllm 2>/dev/null || true
	@echo "→ 임베더 GPU. 색인 후 'docker compose up -d celery'로 CPU 복귀(GPU 반납)"

retrieve-gpu: ## 검색 가속 — api 임베더·리랭커를 GPU로(lite 모드 GPU 놀 때 /retrieve 22s→sub-s). 끝나면 `docker compose up -d api`로 CPU 복귀
	docker compose -f docker-compose.yml -f docker-compose.retrieve-gpu.yml up -d api
	docker compose stop vllm 2>/dev/null || true
	@echo "→ api 임베더·리랭커 GPU(rerank 18.5s→sub-s). vLLM 복귀 전 'docker compose up -d api'로 CPU 반납"



