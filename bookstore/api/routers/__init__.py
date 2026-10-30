"""One router per area of the site.

Split by resource rather than by HTTP verb, so everything that can happen to
a review is in catalog.py and a reviewer can read one file to know the whole
surface of one thing.
"""
