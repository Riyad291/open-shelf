"""The list parser, tested against lines taken from the real files.

Every case below is an actual line (or a trimmed one) from
EbookFoundation/free-programming-books. The format is a convention kept by
hundreds of contributors, so the only trustworthy specification is the data.
"""

from __future__ import annotations

import pytest

from bookstore.ingest.parsers import (
    looks_like_names, normalise_url, parse_entry, parse_markdown_list,
    work_key,
)


def parse(line):
    return parse_entry(line, ["Topic"])


class TestEntryShapes:
    def test_the_plainest_entry(self):
        r = parse("* [The Second Book of Machine Language]"
                  "(https://www.atariarchives.org/2bml/)")
        assert r.title == "The Second Book of Machine Language"
        assert r.url == "https://www.atariarchives.org/2bml"
        assert r.authors == "" and r.media == "" and r.description == ""

    def test_author_and_format(self):
        r = parse("* [Database Management System](https://mrcet.com/x.pdf)"
                  " - Ankit Sharma (PDF)")
        assert r.authors == "Ankit Sharma"
        assert r.media == "PDF"

    def test_several_formats_and_a_licence(self):
        r = parse("* [Ada for the C++ or Java Developer]"
                  "(https://learn.adacore.com/courses/Ada/) - Quentin Ochem "
                  "(HTML, PDF, EPUB) (CC BY-SA)")
        assert r.authors == "Quentin Ochem"
        assert r.media == "HTML, PDF, EPUB"
        assert r.licence == "CC BY-SA"

    def test_a_long_author_list_is_still_authors(self):
        r = parse("* [How to Think Like a Computer Scientist]"
                  "(https://runestone.academy/thinkcspy/) - Brad Miller, "
                  "David Ranum, Jeffrey Elkner, Peter Wentworth, Allen B. "
                  "Downey, Chris Meyers")
        assert r.authors.startswith("Brad Miller")
        assert r.authors.endswith("Chris Meyers")
        assert r.description == ""

    def test_a_sentence_after_the_dash_is_a_description(self):
        r = parse("* [Atariarchives.org](https://www.atariarchives.org) - "
                  "Atariarchives.org makes books, information, and software "
                  "for Atari and other classic computers available on the "
                  "Web.")
        assert r.authors == ""
        assert r.description.startswith("Atariarchives.org makes books")

    def test_a_link_nested_in_the_notes_does_not_break_the_author(self):
        r = parse("* [Build a Full-Stack Web Application Using Angular & "
                  "Firebase](https://www.c-sharpcorner.com/ebooks/x) - Ankit "
                  "Sharma (PDF, [:package: code samples]"
                  "(https://github.com/x))")
        assert r.authors == "Ankit Sharma"

    def test_a_mixed_note_splits_into_media_and_description(self):
        r = parse("* [Build](https://www.c-sharpcorner.com/ebooks/x) - Ankit "
                  "Sharma (PDF, [:package: code samples]"
                  "(https://github.com/x))")
        assert r.media == "PDF"
        assert r.description == "code samples"

    def test_work_in_progress_is_flagged_not_described(self):
        r = parse("* [Mastering Dyalog APL]"
                  "(https://www.dyalog.com/mastering-dyalog-apl.htm) "
                  "(PDF, HTML, IPYNB) *( :construction: in process)*")
        assert r.in_progress is True
        assert r.media.startswith("PDF, HTML")
        assert "construction" not in r.description
        assert "process" not in r.description

    def test_an_institution_counts_as_an_author(self):
        r = parse("* [Database Management System](https://mrcet.com/x.pdf) - "
                  "Malla Reddy College of Engineering and Technology (PDF)")
        assert r.authors == ("Malla Reddy College of Engineering and "
                             "Technology")


class TestRejections:
    @pytest.mark.parametrize("line", [
        "* [0 - Meta-Lists](#0---meta-lists)",
        "### Python",
        "",
        "Some prose about the list.",
        "* Not a link at all",
        "* [Relative](../README.md)",
    ])
    def test_non_entries_are_skipped(self, line):
        assert parse(line) is None

    def test_a_bad_line_costs_only_itself(self):
        text = "\n".join([
            "### Python",
            "* [Good One](https://example.org/one)",
            "* [Broken](not a url",
            "* [Good Two](https://example.org/two)",
        ])
        titles = [r.title for r in parse_markdown_list(text)]
        assert titles == ["Good One", "Good Two"]


class TestHeadings:
    def test_section_and_subsection_become_topics(self):
        text = "\n".join([
            "### Python",
            "* [A](https://example.org/a)",
            "#### Django",
            "* [B](https://example.org/b)",
        ])
        a, b = list(parse_markdown_list(text))
        assert a.topics == ["Python"]
        assert b.topics == ["Python", "Django"]

    def test_the_index_section_is_skipped(self):
        text = "\n".join([
            "### Index",
            "* [Python](https://example.org/index-link)",
            "### Python",
            "* [Real](https://example.org/real)",
        ])
        assert [r.title for r in parse_markdown_list(text)] == ["Real"]


class TestIdentity:
    def test_the_same_line_has_the_same_external_id(self):
        line = "* [A](https://example.org/a)"
        assert parse(line).external_id == parse(line).external_id

    def test_a_trailing_slash_does_not_change_identity(self):
        assert (parse("* [A](https://example.org/a/)").external_id
                == parse("* [A](https://example.org/a)").external_id)

    def test_host_is_case_insensitive_but_path_is_not(self):
        assert (normalise_url("HTTPS://Example.ORG/Path")
                == "https://example.org/Path")
        assert (normalise_url("https://example.org/path")
                != normalise_url("https://example.org/Path"))

    def test_the_same_work_shares_a_work_key(self):
        assert (work_key("The Rust Book", "Steve Klabnik")
                == work_key("the rust book", "Steve Klabnik, Carol Nichols"))

    def test_different_books_have_different_work_keys(self):
        assert (work_key("The Rust Book", "Steve Klabnik")
                != work_key("Rust in Action", "Tim McNamara"))

    def test_the_hash_changes_when_upstream_changes(self):
        before = parse("* [A](https://example.org/a) - Ann Author (PDF)")
        after = parse("* [A](https://example.org/a) - Ann Author (PDF, HTML)")
        assert before.content_hash() != after.content_hash()

    def test_the_hash_is_stable(self):
        line = "* [A](https://example.org/a) - Ann Author (PDF)"
        assert parse(line).content_hash() == parse(line).content_hash()


class TestNameDetection:
    @pytest.mark.parametrize("text", [
        "Allen B. Downey",
        "Brad Miller, David Ranum",
        "Malla Reddy College of Engineering and Technology",
        "Ludwig van Beethoven",
        "Free Ebook Foundation",
    ])
    def test_names(self, text):
        assert looks_like_names(text)

    @pytest.mark.parametrize("text", [
        "a gentle introduction to programming",
        "makes books and software available on the web",
        "the official guide, updated every release",
    ])
    def test_sentences(self, text):
        assert not looks_like_names(text)
