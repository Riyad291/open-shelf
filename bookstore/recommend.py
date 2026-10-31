"""What a recommendation is.

One record, shared by the two places that produce recommendations and the
places that render them. The scoring itself lives in
``services/recommend.py``, in SQL.

There used to be a second implementation here, over the file-backed store,
kept through the move to a database behind the protocols in ``ports.py``.
The application stopped constructing it once every caller had a session,
and a class nothing builds is not an abstraction, it is a copy. Deleted;
``git log`` has it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Recommendation:
    book: dict
    score: float
    reason: str
