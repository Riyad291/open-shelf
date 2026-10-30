"""The HTTP contract.

These are deliberately separate types from the ORM models. Returning ORM
objects directly is convenient and is how password hashes end up in JSON:
the moment a column is added to a table it appears in every response that
serialises that model. Here, a field reaches the client only because somebody
wrote it down.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------- auth


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=4, max_length=128)
    display_name: str = Field("", max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=4, max_length=128)


class SessionOut(Out):
    signed_in: bool
    user_id: Optional[int] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: str
    role_name: str
    warnings: Optional[int] = None
    suspended: bool = False
    trusted: bool = False
    review_count: int = 0
    face_enrolled: bool = False


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: dt.datetime
    session: SessionOut


# -------------------------------------------------------------- catalogue


class FigureOut(Out):
    index: int
    url: str


class BookOut(Out):
    id: int
    title: str
    author: str
    category: str
    year: Optional[int] = None
    description: str = ""
    cover_url: Optional[str] = None
    browse_count: int = 0
    rating: Optional[float] = None
    rating_count: int = 0
    list_status: str = "normal"


class BookDetailOut(BookOut):
    figures: List[FigureOut] = []
    reviews: List["ReviewOut"] = []
    plain_rating: Optional[float] = None


class BookCreateIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    author: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=64)
    year: Optional[int] = Field(None, ge=0, le=2200)
    description: str = ""


class BookUpdateIn(BaseModel):
    """Every field optional: this is a PATCH, not a replace.

    ``exclude_unset`` at the call site is what distinguishes "set this to
    null" from "leave it alone" -- without it a PATCH silently blanks every
    field the client did not mention.
    """
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    author: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, min_length=1, max_length=64)
    year: Optional[int] = Field(None, ge=0, le=2200)
    description: Optional[str] = None


class FacetOut(BaseModel):
    value: str
    count: int


class FacetsOut(BaseModel):
    categories: List[FacetOut]
    authors: List[FacetOut]
    decades: List[FacetOut]


class PageOut(BaseModel):
    """A page of results, with enough to render a pager.

    An endpoint that returns a bare list cannot grow a limit without breaking
    every client, so the catalogue is paginated from the start.
    """
    items: list
    total: int
    limit: int
    offset: int


class SearchHitOut(BaseModel):
    book: BookOut
    score: float
    matched: List[str] = []
    where: str = ""


class ImageHitOut(BaseModel):
    book: BookOut
    score: float
    matched_on: str = "cover"


# ---------------------------------------------------------------- reviews


class ReviewIn(BaseModel):
    stars: int = Field(..., ge=1, le=5)
    comment: str = Field("", max_length=2000)


class ReviewUpdateIn(BaseModel):
    stars: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=2000)


class ReviewOut(Out):
    id: int
    book_id: int
    stars: int
    comment: str
    author: str = "visitor"
    trusted_reviewer: bool = False
    weight: Decimal = Decimal("1.00")
    created_at: dt.datetime


class ReviewResultOut(BaseModel):
    ok: bool
    message: str
    review: Optional[ReviewOut] = None
    blocked: bool = False
    taboo_hits: int = 0
    warning_issued: bool = False
    warnings_total: int = 0
    suspended: bool = False
    demoted: bool = False


# ------------------------------------------------------------------- chat


class ChatIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)


class ChatOut(BaseModel):
    text: str
    source: str
    citations: List[str] = []
    rag_score: float = 0.0
    denied_for_role: bool = False


# ------------------------------------------------------------ biometrics


class BiometricStatusOut(BaseModel):
    face_enrolled: bool
    face_samples: int
    face_model_trained: bool
    detector: str
    required_samples: int
    available: bool
    message: str = ""


class EnrolResultOut(BaseModel):
    ok: bool
    message: str
    accepted: int
    total_samples: int
    trained: bool


# ------------------------------------------------------------- shelf scan


class ShelfMatchOut(BaseModel):
    book: Optional[BookOut] = None
    confidence: float
    region: List[int] = []
    recognised: bool


class ShelfScanOut(BaseModel):
    ok: bool
    message: str = ""
    regions_found: int = 0
    recognised_count: int = 0
    segmentation: str = ""
    matches: List[ShelfMatchOut] = []
    recommendations: List[BookOut] = []
    taste_profile: List[str] = []
    annotated_url: Optional[str] = None


# ------------------------------------------------------------------ admin


class AccountOut(Out):
    id: int
    username: str
    display_name: str
    role: str
    role_name: str
    warnings: int
    suspended: bool
    suspend_reason: str = ""
    face_samples: int = 0
    created_at: dt.datetime


class AccountUpdateIn(BaseModel):
    display_name: Optional[str] = Field(None, max_length=128)
    role: Optional[str] = Field(None, pattern="^(RC|VC|SU)$")


class ReopenIn(BaseModel):
    clear_warnings: bool = False


class SuspendIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255)


class TabooIn(BaseModel):
    word: str = Field(..., min_length=1, max_length=64)


class OrderListEntryOut(BaseModel):
    book_id: int
    title: str
    author: str
    category: str
    count: int
    plain_average: float
    weighted_average: float


class OrderListsOut(BaseModel):
    to_order: List[OrderListEntryOut]
    no_order: List[OrderListEntryOut]


class HealthOut(BaseModel):
    status: str
    database: str
    books: int
    accounts: int
    resources: int
    knowledge_base_chunks: int
    face_model_trained: bool
    face_detector: str
    llm_ready: bool
    llm_status: str


# ------------------------------------------------------------- resources


class TopicOut(BaseModel):
    slug: str
    name: str
    count: int


class ResourceOut(BaseModel):
    id: int
    title: str
    authors: str = ""
    description: str = ""
    url: str
    media: str = ""
    language: str = "en"
    licence: str = ""
    kind: str = "book"
    in_progress: bool = False
    click_count: int = 0
    topics: List[str] = []
    source: str = ""


class ResourceDetailOut(ResourceOut):
    attribution: str = ""
    source_homepage: str = ""
    also_available: List[ResourceOut] = []


class ResourcePageOut(BaseModel):
    items: List[ResourceOut]
    total: int
    limit: int
    offset: int


BookDetailOut.model_rebuild()
