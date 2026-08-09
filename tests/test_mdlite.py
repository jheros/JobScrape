"""The lightweight markdown renderer used for job descriptions on /jobs.

`description_text` is raw third-party text (see the URL-scheme tests in
test_review_fixes.py) that usually carries no literal markdown syntax yet —
`models.html_to_text` flattens HTML to plain text with paragraph breaks only.
These tests cover both: safe escaping of arbitrary scraped text, and correct
rendering for the constructs that do appear.
"""

from __future__ import annotations

from jobscrape.dashboard import mdlite


def test_plain_paragraphs_get_wrapped_and_separated() -> None:
    html = str(mdlite.render("First paragraph.\n\nSecond paragraph."))
    assert html == "<p>First paragraph.</p>\n<p>Second paragraph.</p>"


def test_a_single_newline_within_a_paragraph_is_a_soft_break() -> None:
    html = str(mdlite.render("Line one.\nLine two."))
    assert html == "<p>Line one.<br>Line two.</p>"


def test_empty_and_blank_text_renders_nothing() -> None:
    assert str(mdlite.render(None)) == ""
    assert str(mdlite.render("")) == ""
    assert str(mdlite.render("   \n  \n ")) == ""


def test_raw_html_in_the_source_text_is_escaped_not_executed() -> None:
    """A posting's own text is untrusted (see test_a_dangerous_url_scheme_is_discarded)."""
    html = str(mdlite.render("<script>alert(1)</script> and a <b>tag</b>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html


def test_bullet_list() -> None:
    html = str(mdlite.render("- First\n- Second\n- Third"))
    assert html == "<ul><li>First</li><li>Second</li><li>Third</li></ul>"


def test_numbered_list() -> None:
    html = str(mdlite.render("1. First\n2. Second"))
    assert html == "<ol><li>First</li><li>Second</li></ol>"


def test_a_block_only_becomes_a_list_when_every_line_matches() -> None:
    """One stray non-bulleted line should fall back to a plain paragraph,
    not a list missing an item."""
    html = str(mdlite.render("- First\nNot a bullet\n- Third"))
    assert "<ul>" not in html
    assert "<p>" in html


def test_heading() -> None:
    html = str(mdlite.render("## About the role"))
    assert html == '<p class="md-heading">About the role</p>'


def test_bold_and_italic() -> None:
    html = str(mdlite.render("**bold** and *italic* and _also italic_"))
    assert html == "<p><strong>bold</strong> and <em>italic</em> and <em>also italic</em></p>"


def test_inline_code() -> None:
    html = str(mdlite.render("Run `pytest -q` first"))
    assert html == "<p>Run <code>pytest -q</code> first</p>"


def test_an_https_link_is_rendered_as_a_real_link() -> None:
    html = str(mdlite.render("See [our site](https://example.com/careers)"))
    assert '<a href="https://example.com/careers" target="_blank" rel="noopener">our site</a>' in html


def test_a_dangerous_link_scheme_is_stripped_down_to_plain_text() -> None:
    """Same allowlist as models._safe_link: only http(s) survives."""
    html = str(mdlite.render("[click me](javascript:alert(1))"))
    assert "<a" not in html
    assert "click me" in html


def test_a_scheme_relative_link_is_also_stripped() -> None:
    html = str(mdlite.render("[click me](//evil.example.com/x)"))
    assert "<a" not in html
    assert "click me" in html
