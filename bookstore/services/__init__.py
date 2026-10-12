"""Domain services over a SQLAlchemy session.

Each module here takes a ``Session`` and returns plain results. None of them
open a transaction: the caller owns the unit of work, which for the API is
one HTTP request (see ``db.get_session``). That is what makes checkout
atomic without every function having to think about it.
"""
