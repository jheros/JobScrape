"""The shared, dependency-free HTML fragment extractor `breezy.py` and
`personio.py` use as a fallback for a description that's missing from
their normal feed.
"""

from __future__ import annotations

from jobscrape.sources.html_extract import element_by_id, elements_by_class


def test_elements_by_class_finds_a_simple_block() -> None:
    html = '<div class="other">skip</div><div class="target"><p>Hello</p></div>'
    blocks = elements_by_class(html, "target")
    assert blocks == ["<p>Hello</p>"]


def test_elements_by_class_finds_every_top_level_match_in_order() -> None:
    html = (
        '<div class="item"><h2>First</h2></div>'
        '<div class="item"><h2>Second</h2></div>'
    )
    blocks = elements_by_class(html, "item")
    assert blocks == ["<h2>First</h2>", "<h2>Second</h2>"]


def test_void_elements_do_not_break_the_match() -> None:
    """A bare `<br>` (no closing tag, the overwhelmingly common real-world
    spelling) used to drift the depth counter upward forever, silently
    losing this match and every one after it."""
    html = '<div class="target">Line one<br>Line two<img src="x.png">Line three</div>'
    blocks = elements_by_class(html, "target")
    assert blocks == ["Line one<br>Line two<img src=\"x.png\">Line three"]


def test_void_elements_do_not_break_a_later_match() -> None:
    html = (
        '<div class="target">has a <br> in it</div>'
        '<div class="target">second block</div>'
    )
    blocks = elements_by_class(html, "target")
    assert len(blocks) == 2
    assert "second block" in blocks[1]


def test_a_class_with_multiple_tokens_still_matches() -> None:
    html = '<div class="page_x__abc123 target">content</div>'
    blocks = elements_by_class(html, "target")
    assert blocks == ["content"]


def test_no_match_returns_an_empty_list() -> None:
    assert elements_by_class("<div>nothing here</div>", "missing") == []


def test_element_by_id_finds_the_first_match() -> None:
    html = '<div id="other">skip</div><div id="description">real text</div>'
    assert element_by_id(html, "description") == "real text"


def test_element_by_id_with_no_match_returns_none() -> None:
    assert element_by_id("<div>nothing</div>", "description") is None


def test_malformed_html_does_not_raise() -> None:
    """A truncated or malformed page must not take the whole fetch down —
    whatever was collected before the parser gave up still gets used."""
    html = '<div class="target">unterminated'
    assert elements_by_class(html, "target") == []
