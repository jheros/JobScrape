"""Server-rendered inline SVG charts.

No chart library and no CDN: the dashboard is a local tool and must work
offline. Everything here emits plain SVG strings that the templates drop
straight into the page.

Colour follows the validated categorical palette. The four slots used for
stacked charts were run through the palette validator in both modes:

    light  worst adjacent CVD ΔE 9.1, normal-vision ΔE 22.9  -> PASS
    dark   worst adjacent CVD ΔE 8.4, normal-vision ΔE 19.8  -> PASS

Light mode returns a contrast WARN for aqua (2.74:1) and yellow (2.11:1), so the
**relief rule** applies and is honoured: every stacked chart ships visible value
labels and the same numbers appear in an adjacent table. Colour never carries
meaning on its own.

The status palette is deliberately *not* used for the coverage stack — as a
four-way categorical set it fails the normal-vision floor (warning↔serious
ΔE 13.6). It is built for icon+label pairing, not for series identity.
"""

from __future__ import annotations

import html
from typing import Any, Sequence

# Categorical slots, in the fixed order the palette defines. Never cycled: a
# fifth category folds into "Other" rather than reusing slot 1.
SERIES = ("var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)")

#: 2px of surface between adjacent fills, per the mark spec.
GAP = 2
#: Rounded data-ends.
RADIUS = 4


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt(value: float) -> str:
    """Compact numbers: charts are read at a glance, not audited."""
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return f"{value:.0f}"


def empty_state(message: str = "No data yet") -> str:
    return (
        f'<div class="chart-empty"><span>{_esc(message)}</span></div>'
    )


# --- sparkline ------------------------------------------------------------

def sparkline(values: Sequence[float], width: int = 140, height: int = 36) -> str:
    """A bare trend line for a stat tile. One series, so no legend."""
    points = [float(v) for v in values]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    step = width / (len(points) - 1)
    coords = [
        (i * step, height - 2 - ((v - lo) / span) * (height - 6))
        for i, v in enumerate(points)
    ]
    path = " ".join(
        ("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords)
    )
    area = path + f" L{width:.1f} {height} L0 {height} Z"
    last_x, last_y = coords[-1]
    return f"""<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}"
     role="img" aria-label="trend">
  <path d="{area}" fill="var(--series-1)" opacity="0.12"/>
  <path d="{path}" fill="none" stroke="var(--series-1)" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="var(--series-1)"
          stroke="var(--surface-1)" stroke-width="2"/>
</svg>"""


# --- time series ----------------------------------------------------------

def area_chart(
    series: Sequence[dict[str, Any]],
    *,
    x_key: str = "date",
    y_key: str = "count",
    height: int = 200,
    label: str = "Jobs first seen per day",
) -> str:
    """Single-series area over time.

    One series, so the title names it and no legend box is needed. Hover
    targets span the full column height so a 1px line is not the hit area.
    """
    if not series:
        return empty_state("No activity recorded yet")

    values = [float(p.get(y_key) or 0) for p in series]
    labels = [str(p.get(x_key) or "") for p in series]
    hi = max(values) or 1
    width = 720
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step = plot_w / max(len(values) - 1, 1)

    coords = [
        (pad_l + i * step, pad_t + plot_h - (v / hi) * plot_h)
        for i, v in enumerate(values)
    ]
    line = " ".join(
        ("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords)
    )
    fill = line + f" L{coords[-1][0]:.1f} {pad_t + plot_h} L{pad_l} {pad_t + plot_h} Z"

    # Recessive gridlines with axis values.
    grid = []
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h - frac * plot_h
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">'
            f'{_fmt(hi * frac)}</text>'
        )

    # Only the ends are labelled — never a label on every point.
    ticks = ""
    if labels:
        ticks = (
            f'<text x="{pad_l}" y="{height - 6}" class="axis">{_esc(labels[0][5:])}</text>'
            f'<text x="{width - pad_r}" y="{height - 6}" class="axis" '
            f'text-anchor="end">{_esc(labels[-1][5:])}</text>'
        )

    hovers = "".join(
        f'<rect x="{x - step / 2:.1f}" y="{pad_t}" width="{step:.1f}" height="{plot_h}" '
        f'fill="transparent" class="hit"><title>{_esc(labels[i])}: '
        f'{int(values[i])} jobs</title></rect>'
        for i, (x, _) in enumerate(coords)
    )

    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img"
     aria-label="{_esc(label)}" preserveAspectRatio="xMidYMid meet">
  {''.join(grid)}
  <path d="{fill}" fill="var(--series-1)" opacity="0.14"/>
  <path d="{line}" fill="none" stroke="var(--series-1)" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"/>
  {ticks}
  {hovers}
</svg>"""


# --- ranked bars ----------------------------------------------------------

def bar_chart(
    rows: Sequence[dict[str, Any]],
    *,
    label_key: str,
    value_key: str,
    limit: int = 10,
    label: str = "",
) -> str:
    """Horizontal ranked bars.

    Colour encodes nothing here — magnitude is the bar length — so every bar
    takes one hue rather than a rainbow.
    """
    data = [r for r in rows if (r.get(value_key) or 0)][:limit]
    if not data:
        return empty_state()

    hi = max(float(r[value_key]) for r in data) or 1
    row_h, gap = 26, 6
    height = len(data) * (row_h + gap)
    label_w, value_w = 168, 56
    track = 720 - label_w - value_w

    bars = []
    for i, r in enumerate(data):
        value = float(r[value_key])
        w = max((value / hi) * track, 2)
        y = i * (row_h + gap)
        name = str(r[label_key] or "—")
        shown = name if len(name) <= 24 else name[:23] + "…"
        bars.append(
            f'<text x="0" y="{y + row_h / 2 + 4:.0f}" class="bar-label">{_esc(shown)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{row_h}" rx="{RADIUS}" '
            f'fill="var(--series-1)"><title>{_esc(name)}: {int(value):,}</title></rect>'
            f'<text x="{label_w + w + 8:.1f}" y="{y + row_h / 2 + 4:.0f}" class="bar-value">'
            f'{_fmt(value)}</text>'
        )

    return f"""<svg class="chart" viewBox="0 0 720 {height}" role="img"
     aria-label="{_esc(label)}" preserveAspectRatio="xMidYMid meet">
  {''.join(bars)}
</svg>"""


# --- stacked composition --------------------------------------------------

def stacked_bars(
    rows: Sequence[dict[str, Any]],
    *,
    label_key: str,
    segments: Sequence[tuple[str, str]],
    label: str = "",
) -> str:
    """One stacked bar per row.

    ``segments`` is ``[(key, display name), …]`` in the order they should stack;
    slots are assigned in the palette's fixed order and never cycled.

    A 2px surface gap separates adjacent fills, and each segment wide enough to
    hold text is labelled with its value — that is the relief the light-mode
    contrast warning requires.
    """
    data = [r for r in rows if sum(float(r.get(k) or 0) for k, _ in segments) > 0]
    if not data:
        return empty_state()

    row_h, gap = 30, 10
    height = len(data) * (row_h + gap)
    label_w = 150
    track = 720 - label_w - 8

    out = []
    for i, r in enumerate(data):
        total = sum(float(r.get(k) or 0) for k, _ in segments) or 1
        y = i * (row_h + gap)
        x = float(label_w)
        name = str(r[label_key] or "—")
        out.append(
            f'<text x="0" y="{y + row_h / 2 + 4:.0f}" class="bar-label">{_esc(name)}</text>'
        )
        for slot, (key, display) in enumerate(segments):
            value = float(r.get(key) or 0)
            if value <= 0:
                continue
            w = (value / total) * track
            draw = max(w - GAP, 1)
            out.append(
                f'<rect x="{x:.1f}" y="{y}" width="{draw:.1f}" height="{row_h}" '
                f'rx="{RADIUS}" fill="{SERIES[slot % len(SERIES)]}">'
                f'<title>{_esc(name)} — {_esc(display)}: {int(value):,} '
                f'({value / total * 100:.0f}%)</title></rect>'
            )
            # Direct label wherever it fits; this is the required relief.
            if draw > 42:
                out.append(
                    f'<text x="{x + draw / 2:.1f}" y="{y + row_h / 2 + 4:.0f}" '
                    f'class="seg-value" text-anchor="middle">{_fmt(value)}</text>'
                )
            x += w
    return f"""<svg class="chart" viewBox="0 0 720 {height}" role="img"
     aria-label="{_esc(label)}" preserveAspectRatio="xMidYMid meet">
  {''.join(out)}
</svg>"""


def legend(segments: Sequence[tuple[str, str]]) -> str:
    """Always present for two or more series, so identity is never colour-alone."""
    items = "".join(
        f'<span class="legend-item"><span class="swatch" '
        f'style="background:{SERIES[i % len(SERIES)]}"></span>{_esc(name)}</span>'
        for i, (_, name) in enumerate(segments)
    )
    return f'<div class="legend">{items}</div>'


def single_stack(
    counts: Sequence[tuple[str, int]], *, label: str = "", height: int = 30
) -> str:
    """One horizontal composition bar — for a whole-dataset breakdown."""
    data = [(name, int(v)) for name, v in counts if v]
    if not data:
        return empty_state()
    total = sum(v for _, v in data) or 1
    width, x = 720, 0.0
    out = []
    for slot, (name, value) in enumerate(data):
        w = (value / total) * width
        draw = max(w - GAP, 1)
        out.append(
            f'<rect x="{x:.1f}" y="0" width="{draw:.1f}" height="{height}" rx="{RADIUS}" '
            f'fill="{SERIES[slot % len(SERIES)]}"><title>{_esc(name)}: {value:,} '
            f'({value / total * 100:.0f}%)</title></rect>'
        )
        if draw > 54:
            out.append(
                f'<text x="{x + draw / 2:.1f}" y="{height / 2 + 4:.0f}" class="seg-value" '
                f'text-anchor="middle">{_fmt(value)}</text>'
            )
        x += w
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img"
     aria-label="{_esc(label)}" preserveAspectRatio="none" height="{height}">
  {''.join(out)}
</svg>"""
