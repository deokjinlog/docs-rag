# 아키텍처

## 컴포넌트 구성

```
┌────────────────────────────────────────────────────────────────┐
│                   로컬 개발 머신 (WSL2)                           │
│   RTX 4060 Laptop 8GB · 16-core · Docker · 단일 GPU              │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   Docker Compose                          │  │
│  │                                                          │  │
│  │  ┌────────┐ ┌────────┐ ┌─────────────┐                   │  │
│  │  │  API   │ │Flower  │ │    ODL      │                   │  │
│  │  │(:8002) │ │(:5555) │ │(:5002+:5010)│                   │  │
│  │  │ GPU 0  │ └────────┘ └─────────────┘                   │  │
│  │  └───┬────┘                                              │  │
│  │      │                                                   │  │
│  │  ┌───┴───────────┐  ┌────────────────┐                   │  │
│  │  │ Celery Worker │  │     Paddle     │                   │  │
│  │  │  (threads=4)  │  │    (:5003)     │                   │  │
│  │  │    GPU 0      │  │  ** CPU 고정 **│                   │  │
│  │  │ BGE-M3+Rerank │  │  PPStructureV3 │                   │  │
│  │  └───────────────┘  └────────────────┘                   │  │
│  │                                                          │  │
│  │  ┌────────┐                                              │  │
│  │  │  vLLM  │  GPU 0, TP=1                                 │  │
│  │  │(:8000) │  Qwen3-4B-AWQ, KV fp8 (8GB 프로파일)          │  │
│  │  └────────┘                                              │  │
│  │                                                          │  │
│  │  ┌───────────┐  ┌───────────┐  ┌───────────┐             │  │
│  │  │ RabbitMQ  │  │PostgreSQL │  │  Qdrant   │  GPU 0 인덱싱│  │
│  │  │ (:5672)   │  │ (:5432)   │  │ (:6333)   │             │  │
│  │  └───────────┘  └───────────┘  └───────────┘             │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

- PostgreSQL, Qdrant 모두 docker-compose 내부에서 관리. 데이터는 named volume으로 영속화.
- **vLLM (로컬 8GB 프로파일)**: 단일 GPU, `Qwen3-4B-AWQ`, `--kv-cache-dtype fp8_e4m3`, `--max-model-len`을 8GB에 맞춰 축소(예: 4096), `--gpu-memory-utilization`은 임베더·리랭커와 VRAM을 나눠 쓰도록 조정. **README '평가'의 RAGAS·지연 수치는 대형 GPU + `Qwen3-14B-AWQ` 구성에서 측정** — 로컬은 동일 파이프라인을 소형 모델로 구동(생성 품질은 모델 크기만큼 하락 예상). OpenAI 호환 API LLM으로 교체 시 로컬 LLM VRAM 0.
- **GPU 배치**: 단일 GPU(RTX 4060 8GB)에 임베더(BGE-M3)·리랭커·vLLM(소형)·Qdrant 인덱싱을 공유. VRAM이 빠듯하면 LLM을 API로 빼거나 임베더를 CPU로 오프로드. 다중 GPU 대형 구성은 트래픽↑ 시 확장 지점.
- ODL 컨테이너는 2개 프로세스를 동시 실행:
  - **FastAPI 래퍼** (:5002): `odl/server.py`. Worker가 HTTP로 호출하는 진입점.
    - **원래 존재 이유 (docker.sock 제거)**: 예전엔 celery worker가 `docker exec odl ...`로 PDF 변환을 호출했는데, 이러려면 celery 컨테이너에 `/var/run/docker.sock`을 마운트해야 함 → celery가 호스트 도커 데몬 전체 제어권 확보(다른 컨테이너 kill, privileged 컨테이너 생성 등) → 컨테이너 격리 붕괴 + Kubernetes 이식 불가. 이걸 HTTP 호출로 대체하려고 래퍼를 만든 게 1번 동기.
    - **얹힌 부가 기능**: (a) 2단계 fallback orchestration (docling-fast 품질 검증 실패 시 Java-direct), (b) `/cleanup` API — UID 1000으로 생성된 ODL 파일을 워커가 직접 `rm` 못 하니 컨테이너 내부에서 삭제 경로 제공. 이 둘은 래퍼가 이미 있는 김에 자연스럽게 얹힘.
  - **docling-fast** (:5010): `opendataloader-pdf-hybrid`. Java 엔진이 hybrid 모드에서 내부 호출하는 ML 기반 PDF 변환 백엔드.
- PDF 추출 2단계 전략 (`extract.py`):
  - ≤200p: docling-fast(hybrid) 시도 → 품질 검증 실패 시 Java fallback.
  - >200p: Java-direct (docling-fast 스킵). `DOCLING_PAGE_LIMIT`로 제어.
- **Paddle CPU 고정**: 현재 사용 중인 `paddlepaddle/paddle:3.3.1-gpu-cuda13.0-cudnn9.13` 빌드에 Blackwell sm_120 커널이 포함 안 돼 있어 `paddle.device.cuda.device_count()`가 0으로 떨어지고 `set_device('gpu:0')`이 "Cannot use GPU because there is no GPU detected"로 실패. `CUDA_VISIBLE_DEVICES=-1` + `deploy.resources` 제거로 CPU 모드 고정. Paddle 3.4+ Blackwell 공식 지원 시 환경변수/리소스 블록/`server.py`의 `device="cpu"` 세 군데만 토글로 복귀.
- **Paddle 엔진**: `PPStructureV3(lang="korean", device="cpu", enable_mkldnn=False)` — layout + table + formula + OCR 풀 파이프라인을 CPU 모드로 사용. PIR+oneDNN 경로의 `NotImplementedError` 우회를 위해 `enable_mkldnn=False` (생성자) + `FLAGS_use_mkldnn=0` / `FLAGS_enable_pir_in_executor=0` (env) 3중 차단. 표/수식 구조 정상 추출.
- Paddle은 ODL과 같은 패턴: 별도 Docker 서비스 + HTTP API. 모델 캐시는 볼륨으로 영속화. OCR HTTP 호출은 celery에서 `ThreadPoolExecutor(4)`로 병렬화.
- **Paddle 엔진 싱글톤 ("준비는 1번, 사용은 병렬")**: `paddle/server.py`의 `_get_engine()`. 엔진 초기화만 싱글톤으로 보호하고(`threading.Lock` + double-checked locking), 실제 OCR 추론(`engine.predict`)은 락 밖에서 여러 스레드가 동시에 돌린다 — 엔진은 stateless 추론기라 공유 안전. celery `ThreadPoolExecutor(4)`가 4장을 병렬 OCR로 처리량 올리는 이유가 이거. 덤으로 FastAPI `@app.on_event("startup")` warmup으로 첫 요청이 서브모델 5종 로딩 지연을 떠안지 않게 컨테이너 시작 시점에 생성 앞당김. 이전 버그: lazy init이 락 없어서 celery 첫 배치 4개가 "`_engine is None`" 체크를 동시에 통과 → 4세트 중복 로드로 메모리 피크 OOM 각이었음.
- **Paddle 이미지/패키지 핀**: `paddle/Dockerfile` — base `paddlepaddle/paddle:3.3.1-gpu-cuda13.0-cudnn9.13`, `paddleocr==3.4.0`, `paddlex[ocr]==3.4.3`. 환경변수로 `FLAGS_use_mkldnn=0` + `FLAGS_enable_pir_in_executor=0` 설정 (PIR/oneDNN 경로 예방적 차단).
- Qdrant 설정: on_disk=True, INT8 양자화 (rescore + oversampling 2.0), m=16, ef_construct=100, hnsw_ef=128. GPU 0 인덱싱 활성화, gRPC + `upload_points(parallel=4)` 병렬 업로드.

## 데이터 흐름

### 수집 경로 (비동기)

```
POST /documents
  → PostgreSQL 상태 "00" 등록
  → RabbitMQ에 extract→ocr→chunk→embed 체인 발행 (즉시 응답)

[extract] PDF → ODL (≤200p: docling-fast→fallback Java / >200p: Java-direct)
          → Markdown + 이미지 파일 추출 (ODL image_output="external": PDF 내부 XObject를 파일로 떨굼)
          → PostgreSQL 적재 → finished/ 이동

[ocr]     celery가 markdown의 ![image N](...) 태그 파싱 (paddle은 PDF가 아니라 개별 이미지만 처리)
          [1] is_valid_image 6단계 입구 필터 (file_size, 차원, figure 최소 크기, 비율, 대형, stddev)
              → garbage 이미지(1px/아이콘/단색 등)는 컷, paddle 호출 안 함
          [2] Paddle HTTP → PP-StructureV3 (CPU, layout+table+formula+OCR)
              → _ocr.json/_ocr_layout.png은 원본 그대로 저장 (필터는 청킹 단계에서만)
          [3] _extract_blocks 라벨 분류: drop(header/footer) / table(별도 청크) / 나머지(합쳐서 image 청크)
          [4] is_meaningful_ocr_result + heading 중복 제거
          → _ocr.json / _ocr_layout.png 저장, chunk_type=image/table 청크 생성, 이미지 태그 제거
[chunk]   PostgreSQL Markdown → 청킹 + OCR 청크 합류 → part_index 재부여 → DB 적재
[embed]   PostgreSQL 청크 → BGE-M3 → Qdrant 저장 → PostgreSQL 서빙 데이터 적재

상태: 00 → 22 → 21 → 24 → 23 → 32 → 31 → 42 → 43 → 41 → 11
에러: 91(PDF추출) / 92(OCR) / 93(청킹) / 94(청킹/DB적재) / 95(임베딩) / 96(임베딩/벡터DB적재) / 99(기타)
```

### 서빙 경로

```
POST /retrieve → 쿼리 라우팅 → 하이브리드 검색 → 리랭킹 → 응답
POST /answer   → 쿼리 라우팅 → CRAG 루프 → 프롬프트 분기 → LLM → Self-RAG 검증 → 응답
```

서빙 파이프라인 상세: [pipeline.md](pipeline.md)

## 성능 특성

하드웨어/문서량/쿼리 복잡도에 따라 수치가 크게 달라지므로 구체적 latency는 docs에 박지 않는다. 실측은 아래 도구로 얻고, 결과는 `data/bench/<날짜>/` 아래 보존된다.

### 서빙 경로 (/retrieve, /answer)

- **/retrieve 구간별 비용 (큰 순)**: 리랭킹 (CrossEncoder) > Qdrant 검색 > 쿼리 임베딩 ≈ Sibling 복원
- **/answer = /retrieve + vLLM 생성**. LLM 생성이 전체 latency의 대부분을 차지
- **CRAG 재검색**: 1회당 vLLM 재작성 + 재검색 + 재리랭킹 왕복이 추가됨. `CRAG_MAX_RETRIES=2`로 상한
- **스케일링 포인트**: 청크 수 증가 시 Qdrant HNSW ef 영향으로 검색 latency가 완만히 증가. on_disk=True + INT8 양자화로 메모리 부담은 제한됨

### 수집 경로 (extract → ocr → chunk → embed)

- **병목은 OCR**. paddle CPU 고정 상태라 이미지 많은 문서가 가장 오래 걸림. Blackwell GPU 복귀 시 5~10배 개선 여지
- **PDF 변환**: ≤200p는 docling-fast hybrid, >200p는 Java-direct. 큰 문서가 오히려 더 빠른 역전 현상 발생 가능 (hybrid ML 오버헤드 회피)
- **청킹**: 선형, 가장 저렴
- **임베딩**: BGE-M3 GPU 0 사용. 청크 수에 선형, 1000개 단위 배치
- **텍스트 기반 PDF**: 이미지 garbage가 입구 필터에서 대부분 컷되므로 OCR 부담이 작음. 스캔 PDF / figure 많은 브로셔가 가장 느림

### 실측 도구

| 도구 | 측정 대상 |
|------|----------|
| `scripts/trace_summary.py` | 서빙 경로 trace 12-섹션 집계 (route / decomposition / rerank / CRAG / verification(+groundedness) / provenance / latency / errors / critic / input_guard / output_guard) |
| `scripts/smoke_test.py` | 관측 DoD 11-step 자동 검증 (endpoint + trace schema + aggregation) |
| `scripts/eval_ragas.py` | Faithfulness / Answer Relevancy / Context Utilization + 질문별 응답 시간 (Judge=GPT-4o-mini, Serving=vLLM/Qwen3 분리 — self-preference bias 회피) |
| `scripts/eval_index_health.py` | Dispersion / Confusion Rate / 문서별 벡터 분포 |
| `scripts/eval_ocr.py` | OCR 필터 통과율, confidence 분포, 깨진 텍스트 샘플 |

trace JSONL은 쿼리별 1줄 단위로 `data/eval/trace/YYYYMMDD/`에 append. 집계 결과는 `data/eval/trace_summary_YYYYMMDD.json`.

## 장애 대응

| 장애 | 영향 | 현재 대응 |
|------|------|----------|
| ODL 다운 | 추출만 실패, 검색 정상 | Celery retry 3회 |
| docling-fast 다운 | ≤200p 문서가 Java fallback으로 처리 (품질 차이 없음, 속도만 느림) | extract.py 자동 fallback |
| Qdrant 다운 | 검색 불가 | - |
| vLLM 다운 | /answer만 실패, /retrieve 정상. CRAG 재작성도 불가 | docker compose restart vllm |
| PostgreSQL 다운 | 전체 영향 | - |
| RabbitMQ 다운 | 신규 문서 등록 불가, 처리 중 태스크 유실 가능 | restart: unless-stopped. 현재 단일 인스턴스 + durable queue, 클러스터링/HA는 인프라 스케일-업 시 고려 |
| Worker 다운 | 처리 중단, 큐에 적체 | restart: unless-stopped |

수집/처리/서빙이 RabbitMQ로 분리되어 있어서 한쪽 장애가 다른 쪽을 블로킹하지 않음.
단, RabbitMQ 자체가 단일 브로커로 전체 파이프라인이 큐에 의존. 장기적으로 quorum queue/클러스터링 고려.

## 품질 평가 체계

세 축으로 분리하여 측정한다.

| 축 | 지표 | 도구 | 대상 |
|----|------|------|------|
| 수집 (OCR) | confidence 분포, 필터 통과율 | `scripts/eval_ocr.py` | 이미지 OCR 결과 |
| 수집 (인덱스) | Dispersion, Confusion Rate, 문서별 벡터 분포 | `scripts/eval_index_health.py` | Qdrant 컬렉션 헬스 |
| 서빙 (RAG 품질) | Faithfulness, Answer Relevancy, Context Utilization | `scripts/eval_ragas.py` | 검색+답변 품질 |
| 서빙 (관측) | route 분포, CRAG 전/후 score, risk_level, claim-근거 매핑 coverage, latency | `scripts/trace_summary.py` | 실운영 trace |

## 확장 방향 (인프라 스케일)

```
현재: 단일 서버, Docker Compose
  단일 컬렉션 + service_code 필터, QPS ~10

스케일-업 후보:
  · Worker 큐 분리: extract(CPU)/embed(GPU) 별도 큐로 자원 격리
  · Adaptive 파이프라인 깊이: 쿼리 복잡도에 따라 단계 생략 (예: SIMPLE_FACT는 CRAG 스킵)
  · Qdrant 컬렉션 분리: document_id 스케일이 커지면 service_code별 분리 (Dispersion·Confusion rate 근거)
  · RabbitMQ quorum queue / 클러스터링: 현재 단일 브로커가 SPOF
```
