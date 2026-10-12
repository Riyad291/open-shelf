"""Domain records and role definitions.

Records are plain dictionaries so they serialise straight to the JSON text
files.  The factory functions here are the single source of truth for a
record's shape, which keeps old data files loadable: ``normalise_user`` fills in
any field a newer build added.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, List, Optional

from .storage import utc_now

# ---------------------------------------------------------------------------
# Roles (spec requirement 1).
#
# VISITOR is never stored in users.json -- it is the role the app holds before
# anybody logs in.  The other three are real accounts.
# ---------------------------------------------------------------------------

SUPER_USER = "SU"
VISITOR = "VS"
REGISTERED = "RC"
VIP = "VC"

ROLE_NAMES = {
    SUPER_USER: "Super-user",
    VISITOR: "Visitor",
    REGISTERED: "Registered customer",
    VIP: "VIP customer",
}

#: Privilege ordering used by the chat tiering (requirement 3).  A higher
#: number sees everything a lower number sees, plus more.
ROLE_RANK = {VISITOR: 0, REGISTERED: 1, VIP: 2, SUPER_USER: 3}

#: Roles that must authenticate with a biometric sample (requirement 4).
BIOMETRIC_ROLES = (REGISTERED, VIP, SUPER_USER)

CATEGORIES = [
    "literature",
    "science",
    "history",
    "philosophy",
    "poetry",
    "children",
    "technology",
    "biography",
]


def role_name(role: str) -> str:
    return ROLE_NAMES.get(role, role)


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(minimum, 0)


# ---------------------------------------------------------------------------
# Password hashing.
#
# Not a spec requirement, but storing plaintext passwords in a text file would
# be indefensible even in a course project.  PBKDF2-HMAC-SHA256 from the
# standard library needs no extra dependency.
# ---------------------------------------------------------------------------

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str, salt: Optional[bytes] = None) -> Dict[str, str]:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return {"salt": salt.hex(), "password_hash": digest.hex()}


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    """Check a password against a stored salt and hash.

    Returns ``False`` for any malformed stored credential rather than raising:
    a hand-edited or truncated ``users.json`` should refuse the login, not
    crash the sign-in screen.
    """
    if not salt_hex or not expected_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USER_DEFAULTS: Dict[str, Any] = {
    "user_id": "",
    "username": "",
    "display_name": "",
    "salt": "",
    "password_hash": "",
    "role": REGISTERED,
    "balance": 0.0,
    "warnings": 0,
    "suspended": False,
    "suspend_reason": "",
    # Set when a VC is demoted for warnings.  Without it the promotion rule
    # (requirement 11) would immediately re-promote them on their old history.
    "vip_ineligible": False,
    "purchase_count": 0,
    "total_spent": 0.0,
    "total_fines": 0.0,
    "face_enrolled": False,
    "voice_enrolled": False,
    "browsing_history": [],   # list of {book_id, at}
    "purchase_history": [],   # list of {book_id, price_paid, at}
    "created_at": "",
}


def new_user(
    user_id: str,
    username: str,
    password: str,
    role: str = REGISTERED,
    balance: float = 0.0,
    display_name: str = "",
) -> dict:
    record = dict(USER_DEFAULTS)
    record.update(
        {
            "user_id": user_id,
            "username": username.strip(),
            "display_name": display_name.strip() or username.strip(),
            "role": role,
            "balance": round(float(balance), 2),
            "created_at": utc_now(),
            "browsing_history": [],
            "purchase_history": [],
        }
    )
    record.update(hash_password(password))
    return record


def normalise_user(user: dict) -> dict:
    """Fill in any missing field so older data files stay loadable."""
    for key, default in USER_DEFAULTS.items():
        if key not in user:
            user[key] = [] if isinstance(default, list) else default
    return user


def is_account_role(role: str) -> bool:
    return role in (SUPER_USER, REGISTERED, VIP)


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

BOOK_DEFAULTS: Dict[str, Any] = {
    "book_id": "",
    "title": "",
    "author": "",
    "category": "literature",
    "year": 0,
    "price": 0.0,
    "stock": 0,
    "description": "",
    "cover_path": "",
    "figure_paths": [],
    "text_path": "",
    "browse_count": 0,
    "purchase_count": 0,
    "added_at": "",
}


def new_book(
    book_id: str,
    title: str,
    author: str,
    category: str,
    year: int,
    price: float,
    stock: int = 10,
    description: str = "",
    cover_path: str = "",
    figure_paths: Optional[List[str]] = None,
    text_path: str = "",
) -> dict:
    record = dict(BOOK_DEFAULTS)
    record.update(
        {
            "book_id": book_id,
            "title": title.strip(),
            "author": author.strip(),
            "category": (category or "literature").strip().lower(),
            "year": int(year or 0),
            "price": round(float(price), 2),
            "stock": int(stock),
            "description": description.strip(),
            "cover_path": cover_path,
            "figure_paths": list(figure_paths or []),
            "text_path": text_path,
            "added_at": utc_now(),
        }
    )
    return record


def normalise_book(book: dict) -> dict:
    for key, default in BOOK_DEFAULTS.items():
        if key not in book:
            book[key] = [] if isinstance(default, list) else default
    return book


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def new_review(
    review_id: str,
    book_id: str,
    user_id: str,
    role_at_time: str,
    stars: int,
    raw_comment: str,
    masked_comment: str,
    taboo_hits: int,
    blocked: bool,
    verified_purchase: bool,
    weight: float,
) -> dict:
    return {
        "review_id": review_id,
        "book_id": book_id,
        "user_id": user_id,
        "role_at_time": role_at_time,
        "stars": int(stars),
        # The original text is kept for the SU moderation view only; the GUI
        # shows ``comment`` to everybody else.
        "raw_comment": raw_comment,
        "comment": masked_comment,
        "taboo_hits": int(taboo_hits),
        "blocked": bool(blocked),
        "verified_purchase": bool(verified_purchase),
        "weight": float(weight),
        "created_at": utc_now(),
    }
