"""Pull one named element's inner HTML out of a raw page, via the stdlib
parser rather than a soup library — this project has no HTML-parsing
dependency, and a single shared helper is cheaper than adding one for what
only a couple of adapters need.

Built for the description-page fallback both `breezy.py` and `personio.py`
use when their normal feed comes back with no description: Breezy's own
posting page no longer embeds JSON-LD for some tenants, and Personio's XML
feed ships `<jobDescriptions></jobDescriptions>` empty for some tenants even
though the same posting's own page has the real text. Reusing the page
markup itself, rather than guessing at a second API, survives redesigns
better than nothing and costs no extra request when the HTML is already in
hand (Breezy fetches it anyway, for JSON-LD).
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Callable


class _ElementCollector(HTMLParser):
    """Collects the inner HTML of every top-level element `match` accepts.

    "Top-level" meaning: once inside a match, nested elements of the same
    kind are collected as part of it, not treated as separate matches —
    the same nesting a real DOM query would respect.
    """

    #: HTML5 void elements — no closing tag, ever, whether or not the source
    #: bothered with a trailing `/>`. `HTMLParser` only routes to
    #: `handle_startendtag` for the explicit-slash spelling; a bare `<br>`
    #: (the overwhelmingly common one in real rich-text bodies) still comes
    #: through `handle_starttag` with no `handle_endtag` ever following it —
    #: depth-counting without this list drifts upward on the first one and
    #: never returns to zero, silently losing every match after it.
    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self, match: Callable[[str, dict[str, str | None]], bool]):
        super().__init__(convert_charrefs=False)
        self._match = match
        self._depth = 0
        self._chunks: list[str] = []
        self.results: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            if self._depth > 0:
                self._chunks.append(self.get_starttag_text() or f"<{tag}>")
            return
        if self._depth > 0:
            self._depth += 1
            self._chunks.append(self.get_starttag_text() or f"<{tag}>")
        elif self._match(tag, dict(attrs)):
            self._depth = 1
            self._chunks = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth > 0:
            self._chunks.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self.results.append("".join(self._chunks))
        else:
            self._chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._depth > 0:
            self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._depth > 0:
            self._chunks.append(f"&#{name};")


def _has_class(attrs: dict[str, str | None], cls: str) -> bool:
    return cls in (attrs.get("class") or "").split()


def elements_by_class(html: str, cls: str) -> list[str]:
    """Every top-level element's inner HTML, for elements whose class list
    includes `cls`. Several same-class blocks (e.g. Personio's repeated
    "job description item" sections) all come back, in document order."""
    collector = _ElementCollector(lambda _tag, attrs: _has_class(attrs, cls))
    try:
        collector.feed(html)
    except Exception:
        # A malformed page shouldn't take the whole fetch down with it —
        # whatever was collected before the parser choked is still used.
        pass
    return collector.results


def element_by_id(html: str, elem_id: str) -> str | None:
    """The first element's inner HTML with this exact `id`, or `None`."""
    collector = _ElementCollector(lambda _tag, attrs: attrs.get("id") == elem_id)
    try:
        collector.feed(html)
    except Exception:
        pass
    return collector.results[0] if collector.results else None
