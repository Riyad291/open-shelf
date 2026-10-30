"""Importing free resources from catalogues other people maintain.

Three parts, kept apart on purpose:

* ``parsers``  -- text in, plain records out. No database, so it can be
  tested against real upstream lines without any setup.
* ``pipeline`` -- records in, rows written. Idempotent: running the same
  import twice changes nothing the second time.
* ``sources``  -- one module per catalogue: where to fetch it, what licence
  it is under and how to credit it.

Parsing is where the surprises are; writing is where the damage is.
"""
