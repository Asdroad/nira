"""Model output is data, never executable instructions or permission to send."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Structured(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Citation(Structured):
    fragment_id: str
    excerpt: str = Field(min_length=1, max_length=1800)


class Fact(Structured):
    value: str | None = Field(max_length=3000)
    evidence: list[Citation] = Field(max_length=10)


class Criterion(Structured):
    id: str = Field(min_length=1, max_length=50)
    requirement: str = Field(min_length=1, max_length=500)
    required: bool
    task_excerpt: str = Field(min_length=1, max_length=1000)


class Check(Structured):
    criterion_id: str
    status: Literal["matches", "does_not_match", "unknown"]
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: list[Citation] = Field(max_length=10)


class Offer(Structured):
    document_id: str
    role: Literal["offer", "price_list", "specification", "other"]
    supplier: Fact
    product: Fact
    quantity: Fact
    price: Fact
    tax: Fact
    delivery: Fact
    payment: Fact
    validity: Fact
    total_amount: float | None = Field(ge=0)
    currency: Literal["RUB", "USD", "EUR", "CNY"] | None
    price_basis: Literal["total", "unit", "unknown"]
    tax_included: bool | None
    tax_amount: float | None = Field(ge=0)
    delivery_included: bool | None
    delivery_cost: float | None = Field(ge=0)
    delivery_days: int | None = Field(ge=0, le=3650)
    valid_until: str | None
    checks: list[Check] = Field(max_length=100)
    strengths: list[str] = Field(max_length=10)
    risks: list[str] = Field(max_length=20)
    clarification_questions: list[str] = Field(max_length=20)


class RankedOffer(Structured):
    document_id: str
    reason: str = Field(min_length=1, max_length=3000)


class PreparedText(Structured):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    missing_information: list[str] = Field(max_length=30)


class LeadAssessment(Structured):
    source_id: str
    relevance: Literal["potential", "irrelevant", "needs_check"]
    entity_type: Literal["supplier", "customer", "marketplace", "other", "unknown"]
    activity_excerpt: str | None = Field(max_length=1500)
    reason: str = Field(min_length=1, max_length=2000)
    company_name: str | None = Field(max_length=200)
    company_excerpt: str | None = Field(max_length=1000)
    email: str | None = Field(max_length=254)


class Analysis(Structured):
    summary: str = Field(min_length=1, max_length=5000)
    criteria: list[Criterion] = Field(max_length=100)
    offers: list[Offer] = Field(max_length=30)
    ranking: list[RankedOffer] = Field(max_length=30)
    recommended_document_id: str | None
    recommendation_reason: str = Field(max_length=5000)
    announcement: PreparedText
    email: PreparedText
    search_queries: list[str] = Field(max_length=5)
    leads: list[LeadAssessment] = Field(max_length=50)
    questions: list[str] = Field(max_length=30)


class LeadAnalysis(Structured):
    leads: list[LeadAssessment] = Field(max_length=50)
