"""Request and response models for the HTTP API.

These are the API's contract, kept deliberately separate from the internal
record shapes in ``models.py``. A stored user record carries a password hash,
a salt and a browsing history; none of that belongs in an HTTP response, and
pinning the contract here means an internal field added later is not leaked by
accident.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- books


class BookOut(BaseModel):
    book_id: str
    title: str
    author: str
    category: str
    year: int
    price: float = Field(..., description="List price before any discount")
    your_price: float = Field(..., description="Price for the current session, "
                                               "with the VIP discount applied")
    stock: int
    description: str = ""
    cover_url: Optional[str] = None
    rating: Optional[float] = Field(None, description="Weighted average, "
                                                      "null when unrated")
    rating_count: int = 0
    list_status: str = Field(..., description="normal, no-order or to-order")


class BookDetail(BookOut):
    plain_rating: Optional[float] = None
    verified_buyers: int = 0
    browse_count: int = 0
    purchase_count: int = 0
    figures: List[str] = []
    reviews: List["ReviewOut"] = []


class RecommendationOut(BaseModel):
    book: BookOut
    reason: str


# ---------------------------------------------------------------- reviews


class ReviewIn(BaseModel):
    stars: int = Field(..., ge=1, le=5)
    comment: str = ""


class ReviewOut(BaseModel):
    review_id: str
    author: str
    stars: int
    comment: str
    verified_purchase: bool
    taboo_hits: int
    created_at: str


class ReviewResponse(BaseModel):
    ok: bool
    message: str
    blocked: bool = False
    taboo_hits: int = 0
    warning_issued: bool = False
    warnings_total: int = 0
    suspended: bool = False
    demoted: bool = False


# ---------------------------------------------------------------- accounts


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)
    password: str = Field(..., min_length=4)
    display_name: str = ""
    opening_deposit: float = Field(25.0, ge=0)


class LoginIn(BaseModel):
    username: str
    password: str


class SessionOut(BaseModel):
    signed_in: bool
    user_id: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: str
    role_name: str
    balance: Optional[float] = None
    warnings: Optional[int] = None
    purchase_count: Optional[int] = None
    total_spent: Optional[float] = None
    vip_discount: Optional[float] = None
    face_enrolled: bool = False


class PurchaseRecordOut(BaseModel):
    """One line of a customer's own purchase history."""
    book_id: str
    title: str
    author: str = ""
    price_paid: float
    at: str = ""
    cover_url: Optional[str] = None


class PurchaseResponse(BaseModel):
    ok: bool
    message: str
    price_paid: float = 0.0
    fine_applied: float = 0.0
    new_balance: float = 0.0
    promoted: bool = False
    suspended: bool = False


class DepositIn(BaseModel):
    amount: float = Field(..., gt=0)


# ---------------------------------------------------------------- chat


class ChatIn(BaseModel):
    question: str = Field(..., min_length=1)


class ChatOut(BaseModel):
    text: str
    source: str = Field(..., description="store data, via RAG, via LLM, "
                                         "access denied or unavailable")
    citations: List[str] = []
    rag_score: float = 0.0
    denied_for_role: bool = False


# ---------------------------------------------------------------- catalogue


class FacetOut(BaseModel):
    value: str
    count: int


class SearchHitOut(BaseModel):
    book: BookOut
    score: float
    matched: List[str] = []
    where: str = ""


class ImageHitOut(BaseModel):
    book: BookOut
    similarity: float
    kind: str


class BookIn(BaseModel):
    title: str = Field(..., min_length=1)
    author: str = Field(..., min_length=1)
    category: str = "literature"
    year: int = 0
    price: float = Field(..., ge=0)
    stock: int = Field(10, ge=0)
    description: str = ""


# ---------------------------------------------------------------- admin


class OrderListRow(BaseModel):
    book: BookOut
    count: int
    average: float
    plain_average: float


class OrderListsOut(BaseModel):
    no_order: List[OrderListRow]
    to_order: List[OrderListRow]


class FinanceOut(BaseModel):
    revenue_sales: float
    revenue_fines: float
    refunds: float
    total_revenue: float
    purchase_count: int
    fine_count: int
    average_order: float
    customer_liability: float
    inventory_value: float
    suspended_count: int
    negative_balance_count: int
    top_books: List[dict] = []
    revenue_by_category: List[dict] = []
    user_counts: dict = {}


class AccountOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str
    role_name: str
    balance: float
    total_spent: float
    purchase_count: int
    total_fines: float
    warnings: int
    suspended: bool
    suspend_reason: str = ""
    face_samples: int = 0


class TabooIn(BaseModel):
    word: str = Field(..., min_length=1)


class HealthOut(BaseModel):
    status: str
    books: int
    accounts: int
    knowledge_base_chunks: int
    covers_indexed: int
    llm_ready: bool
    llm_status: str
    face_model_trained: bool
    face_detector: str


BookDetail.model_rebuild()
