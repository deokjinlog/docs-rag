# docs-rag — host에서 직접 실행하는 dev 서버 + 평가·테스트 명령.
# (docker / uv / 기본 git 명령은 표준이라 별도 alias 두지 않음.)

include .env
export

.PHONY: api celery flower \
        test test-host test-integration test-rag test-guards \
        eval feedback-submit trace trace-feedback smoke eval-ocr eval-index


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
