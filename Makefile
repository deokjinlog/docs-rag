# docs-rag — host에서 직접 실행하는 dev 서버 + 평가·테스트 명령.
# (docker / uv / 기본 git 명령은 표준이라 별도 alias 두지 않음.)

include .env
export

.PHONY: api celery flower \
        test test-host test-integration test-rag test-guards \
        eval eval-retrieval chunk-quality eval-routing eval-sql-routing feedback-submit trace trace-feedback smoke eval-ocr eval-index bench \
        mem watch recover lite ingest answer full


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


# ─── Eval & Observability ─────────────────────────────────────────────────

eval: ## RAGAS Triad 평가 (Judge=GPT-4o-mini 권장 — OPENAI_API_KEY env 필요. --basic 플래그는 직접 호출)
	uv run python scripts/eval_ragas.py

eval-retrieval: ## 검색 골든셋 recall@k · MRR (스택 필요 — /retrieve 호출. --update-baseline로 기준선 고정)
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

lite: ## 경량(~2GB) — vLLM·paddle·odl 중지. 검색/관계형/make check/eval + Ralph 공존
	docker compose stop vllm paddle odl
	@$(MAKE) --no-print-directory mem

ingest: ## 색인용(~3.7GB) — paddle·odl 켜고 vLLM은 끔(파이프라인은 LLM 불필요). Ralph 공존 가능
	docker compose start paddle odl 2>/dev/null || docker compose up -d paddle odl
	docker compose stop vllm 2>/dev/null || true
	@echo "→ extract→ocr→chunk→embed 파이프라인 가능. 끝나면 make lite로 반납"

answer: ## /answer용(~4.5GB) — vLLM 켜고 paddle·odl은 끔 (vLLM 재로드 1~2분)
	docker compose start vllm 2>/dev/null || docker compose up -d vllm
	docker compose stop paddle odl 2>/dev/null || true
	@echo "→ vLLM 재로드 1~2분 후 /answer 가능"

full: ## 전체(~6.2GB) — 색인+/answer 동시 (Ralph 공존 빠듯)
	docker compose start vllm paddle odl 2>/dev/null || docker compose up -d vllm paddle odl
	@echo "→ vLLM 재로드 1~2분 후 /answer 가능"
