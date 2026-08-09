"""A minimal, dependency-free Markdown-ish renderer for job descriptions.

`description_text` is untrusted third-party content (see the URL-scheme
tests in `tests/test_review_fixes.py`) that has already been flattened from
HTML to plain text (`models.html_to_text` drops all inline markup, keeping
only paragraph breaks) — so most stored rows carry no literal markdown
syntax yet. This still parses the common constructs (bullets, numbered
lists, headings, bold/italic/code, links) so any text that *does* carry them
renders properly, and it always turns a blank line into a real paragraph
break, which the raw plain-text display never did either way.

No third-party markdown library: this codebase deliberately keeps its
dependency footprint small (see pyproject.toml's own comment on excel export
not pulling in pandas/numpy), and the constructs actually worth supporting
here are a small, fixed set.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)")
_BULLET_RE = re.compile(r"^[-*•]\s+(.*)")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)")


def _link_sub(m: re.Match[str]) -> str:
    label, url = m.group(1), m.group(2)
    # Same http(s)-only allowlist as models._safe_link: a posting is raw
    # third-party text, and this origin has no auth on its mutating routes.
    if url.lower().startswith(("http://", "https://")):
        return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'
    return label


def _bold_sub(m: re.Match[str]) -> str:
    return f"<strong>{m.group(1) or m.group(2)}</strong>"


def _italic_sub(m: re.Match[str]) -> str:
    return f"<em>{m.group(1) or m.group(2)}</em>"


def _inline(text: str) -> str:
    """Escape raw text, then layer on inline formatting.

    Order matters: code first (so its contents can't be re-parsed as a link
    or emphasis), then links, then bold before italic (so italic's
    single-`*` pattern never sees a `**` pair bold already consumed).
    """
    out = str(escape(text))
    out = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _LINK_RE.sub(_link_sub, out)
    out = _BOLD_RE.sub(_bold_sub, out)
    out = _ITALIC_RE.sub(_italic_sub, out)
    return out


def _render_block(block: str) -> str:
    lines = block.split("\n")

    if len(lines) == 1:
        heading = _HEADING_RE.match(lines[0])
        if heading:
            return f'<p class="md-heading">{_inline(heading.group(1))}</p>'

    bullets = [_BULLET_RE.match(line) for line in lines]
    if all(bullets):
        items = "".join(f"<li>{_inline(b.group(1))}</li>" for b in bullets)
        return f"<ul>{items}</ul>"

    numbered = [_NUMBERED_RE.match(line) for line in lines]
    if all(numbered):
        items = "".join(f"<li>{_inline(n.group(1))}</li>" for n in numbered)
        return f"<ol>{items}</ol>"

    return f"<p>{_inline(block).replace(chr(10), '<br>')}</p>"


def render(text: str | None) -> Markup:
    """Render job-description plain text as safe, lightly-formatted HTML."""
    if not text or not text.strip():
        return Markup("")
    blocks = re.split(r"\n\s*\n", text.strip())
    return Markup("\n".join(_render_block(b.strip()) for b in blocks if b.strip()))
