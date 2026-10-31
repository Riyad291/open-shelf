"""Shelf Scan, over HTTP.

Written because the endpoint had no test at all. A rename inside
``imagesearch`` broke it -- the scanner reaches through the index to find
the models directory -- and the whole suite stayed green while the feature
returned a 500 in the browser. A unit test of the segmentation would not
have caught it either: the break was in how the router wires the pieces
together, so the test has to go through the router.

The covers here are flat colours rather than the seeder's rendered ones.
That is enough: the descriptor has to recognise an image as itself, and
three obviously different colours make the ranking unambiguous without
pulling the demo data generator into the test.
"""

from __future__ import annotations

import os

import pytest

from bookstore.db import models as m

COLOURS = [(190, 40, 40), (40, 150, 70), (50, 70, 200)]


def _shelf(session, workspace):
    """Three books, each with a cover image of its own on disk."""
    Image = pytest.importorskip("PIL.Image", reason="covers need Pillow")

    covers = os.path.join(workspace, "data", "covers")
    os.makedirs(covers, exist_ok=True)

    books = []
    for index, colour in enumerate(COLOURS):
        path = os.path.join(covers, "shelf%02d.png" % index)
        image = Image.new("RGB", (240, 360), colour)
        # A lighter band across the middle, in a different place on each
        # cover, so the descriptor has some structure to tell them apart
        # and not only the average colour.
        band = Image.new("RGB", (240, 40),
                         tuple(min(255, c + 60) for c in colour))
        image.paste(band, (0, 40 + index * 90))
        image.save(path)

        book = m.Book(title="Colour %d" % index, author="A Tester",
                      category="testing", year=2000 + index,
                      cover_path=path)
        session.add(book)
        session.flush()
        books.append((book.id, path))

    session.commit()
    return books


def test_a_photographed_cover_is_recognised(client, session, workspace):
    books = _shelf(session, workspace)
    book_id, cover = books[0]

    with open(cover, "rb") as handle:
        reply = client.post(
            "/api/shelf-scan",
            files={"photo": ("shelf.png", handle.read(), "image/png")})

    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["ok"] is True
    assert body["regions_found"] >= 1

    found = [match["book"]["id"] for match in body["matches"]
             if match.get("book")]
    assert book_id in found


def test_the_photo_field_is_required(client):
    """Posting nothing is a bad request, not a server error."""
    assert client.post("/api/shelf-scan").status_code == 422
