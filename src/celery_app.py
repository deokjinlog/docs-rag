"""Celery 설정 (broker = RabbitMQ).

운영 결정:
  - acks_late=True + prefetch_multiplier=1: worker가 task 처리 후에 ack →
    중간에 worker 죽어도 task 유실 없음 (대신 멱등성 책임은 task 쪽에).
  - backend="rpc://": result는 거의 안 씀 (체인 prev_result만 전달). DB/Redis
    backend 추가 의존성 회피.
  - timezone="Asia/Seoul" + enable_utc=False: Celery beat schedule이 한국 시간
    기준으로 해석되도록 (현재 beat 미사용이지만 추후 도입 대비).
"""

import os
from celery import Celery

RABBITMQ_URL = os.environ["RABBITMQ_URL"]

celery_app = Celery(
    "docs_rag",
    broker=RABBITMQ_URL,
    backend="rpc://",
    include=[
        "v1.task.extract",
        "v1.task.ocr",
        "v1.task.chunk",
        "v1.task.embed",
    ],
)

celery_app.conf.update(
    timezone="Asia/Seoul",
    enable_utc=False,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    result_expires=3600,
    task_default_retry_delay=60,
    task_max_retries=3,
    # OCR 만 별도 큐로 — paddle(GPU) 이 `ocr` 프로필에서만 뜨기 때문이다(D1 메모리 예산).
    # 이 라우팅이 없으면 ingest 워커가 OCR 태스크를 가져가서 paddle 없이 실패한다.
    # ocr 워커가 안 떠 있으면 태스크는 큐에 **대기**한다 — 조용히 건너뛰지 않는다.
    task_routes={"v1.task.ocr.ocr_images": {"queue": "ocr"}},
)
