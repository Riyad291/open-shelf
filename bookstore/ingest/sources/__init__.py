"""The catalogues we import from, by slug."""

from . import free_programming_books

REGISTRY = {free_programming_books.SLUG: free_programming_books}
