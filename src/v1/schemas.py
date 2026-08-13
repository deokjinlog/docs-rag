"""API Request/Response Pydantic 스키마.

max_length·ge·le 같은 입력 검증은 여기서만 강제 (router는 검증 통과한 객체만
받음). 길이 제약을 늘릴 때는 DB 컬럼 길이(models.py·schema.sql)와 함께
확인해야 silent truncate 회피.
"""

from typing import Literal

from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    service_code: str = Field(..., examples=["01"], max_length=10)
    document_id: str = Field(..., examples=["0001"], max_length=255)
    document_name: str = Field(..., examples=["약관.pdf"], max_length=500)
    document_path: str | None = Field(None, examples=["/path"], max_length=500)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., examples=[["보험금 지급 조건"]], max_length=100)


class AnswerRequest(BaseModel):
    query: str = Field(..., examples=["보험금 지급 조건"], max_length=2000)
    service_code: str | None = Field(None, examples=["01"], max_length=10)
    document_id: str | None = Field(None, examples=["0001"], max_length=255)
    start_page: int | None = Field(None, examples=[1], ge=1, le=99999)
    end_page: int | None = Field(None, examples=[50], ge=1, le=99999)
    include_keywords: list[str] | None = Field(None, examples=[["입원", "보상"]], max_length=20)
    exclude_keywords: list[str] | None = Field(None, examples=[["면책"]], max_length=20)
    top_k: int = Field(3, ge=1, le=20)


class RetrieveRequest(BaseModel):
    query: str = Field(..., examples=["입원비 보상 한도가 어떻게 되나요?"], max_length=2000)
    service_code: str | None = Field(None, examples=["01"], max_length=10)
    document_id: str | None = Field(None, examples=["0001"], max_length=255)
    start_page: int | None = Field(None, examples=[1], ge=1, le=99999)
    end_page: int | None = Field(None, examples=[50], ge=1, le=99999)
    include_keywords: list[str] | None = Field(None, examples=[["입원", "보상"]], max_length=20)
    exclude_keywords: list[str] | None = Field(None, examples=[["면책"]], max_length=20)
    top_k: int = Field(10, ge=1, le=100)


class PayoutRequest(BaseModel):
    """POST /payout 요청 body — 결정론 SQL 경로("얼마/언제").

    query: 지급 관련 질의 (예: "중환자실 입원하면 하루 얼마?")
    product_id: 특정 상품으로 한정 (예: LINA_ICU_2024). 미지정 시 전 상품 대상
    """
    query: str = Field(..., examples=["중환자실 입원하면 하루 얼마 받아요?"], max_length=2000)
    service_code: str | None = Field(None, examples=["01"], max_length=10)
    product_id: str | None = Field(None, examples=["LINA_ICU_2024"], max_length=64)


class FeedbackRequest(BaseModel):
    """POST /feedback 요청 body.

    trace_id: /answer·/retrieve 응답의 trace_id 그대로
    signal: up(좋음) / down(나쁨) / reformulated(재질문). Literal로 422 자동 검증
    free_text: 선택적 자유 서술 (max 2000자)
    """
    trace_id: str = Field(..., examples=["abc-123-def-456"], min_length=1, max_length=64)
    signal: Literal["up", "down", "reformulated"] = Field(..., examples=["up"])
    free_text: str | None = Field(None, examples=["근거가 부족함"], max_length=2000)


class FeedbackResponse(BaseModel):
    """POST /feedback 응답."""
    id: int
    stored_at: str  # ISO 8601 UTC
