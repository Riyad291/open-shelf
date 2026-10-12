"""One router per area of the shop.

Split by resource rather than by HTTP verb, so everything that can happen to
a cart is in cart.py and a reviewer can read one file to know the whole
surface of one thing.
"""
