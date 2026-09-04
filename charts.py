"""
Small hand-rolled SVG chart helpers - no JS chart library, no CDN, so the
dashboard keeps working with zero external dependencies at runtime.

Follows the house rules for this kind of chart: thin 2px lines, >=8px end
markers with a surface-color ring, capped/rounded bars with a gap between
them, a legend whenever there's more than one series, sparse direct labels
(not a number crammed onto every point), and a native <title> tooltip on
every mark so hovering still tells you the exact value.
"""

import html
from markupsafe import Markup

PAD_LEFT = 44
PAD_RIGHT = 16
PAD_TOP = 16
PAD_BOTTOM = 34
SURFACE = "var(--surface)"


def _esc(s):
    return html.escape(str(s), quote=True)


def _nice_ticks(lo, hi, count=4, integer=False):
    if lo == hi:
        lo -= 1
        hi += 1
    span = hi - lo
    step = span / count
    if integer:
        # whole-number data (e.g. pick counts) never gets a fractional
        # gridline like 0.1 - the smallest useful step is 1.
        step = max(1, round(step))
    else:
        # round the step to a "clean" increment
        magnitude = 10 ** (len(str(int(step))) - 1) if step >= 1 else 0.1
        for candidate in (magnitude, magnitude / 2, magnitude / 5, magnitude / 10):
            if candidate <= step:
                step = candidate
                break
    ticks = []
    start = int(lo / step) * step
    v = start
    while v <= hi + step * 0.001:
        if v >= lo - step * 0.001:
            ticks.append(round(v, 3))
        v += step
    return ticks or [lo, hi]


def line_chart(categories, series, width=680, height=260, unit="", y_min=None, y_max=None):
    """
    categories: list of x-axis labels (e.g. meet dates), left-to-right.
    series: list of dicts: {"name", "color", "values"} - values aligns with
            categories 1:1; None for a category the series has no point at.
    """
    all_vals = [v for s in series for v in s["values"] if v is not None]
    if not all_vals:
        return Markup('<p class="chart-empty">No data yet.</p>')

    lo = y_min if y_min is not None else min(all_vals)
    hi = y_max if y_max is not None else max(all_vals)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    plot_w = width - PAD_LEFT - PAD_RIGHT
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = len(categories)
    step_x = plot_w / max(n - 1, 1)

    def x_of(i):
        return PAD_LEFT + step_x * i

    def y_of(v):
        return PAD_TOP + plot_h - (v - lo) / (hi - lo) * plot_h

    ticks = _nice_ticks(lo, hi)
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="Line chart">'
    ]

    for t in ticks:
        y = y_of(t)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{width - PAD_RIGHT}" y2="{y:.1f}" '
            f'class="grid-line" />'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 8}" y="{y:.1f}" class="axis-label" '
            f'text-anchor="end" dominant-baseline="middle">{t:g}{unit}</text>'
        )

    label_every = max(1, n // 6)
    for i, cat in enumerate(categories):
        if i % label_every == 0 or i == n - 1:
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            parts.append(
                f'<text x="{x_of(i):.1f}" y="{height - PAD_BOTTOM + 18}" '
                f'class="axis-label" text-anchor="{anchor}">{_esc(cat)}</text>'
            )

    # Direct end-labels work when a single line has the chart to itself. With
    # several series, their endpoints tend to converge at the right edge -
    # stacking labels there just collides and reads as noise. In that case
    # the legend (rendered below) plus each point's hover tooltip carry the
    # value instead, per the house rule on converging series.
    label_endpoints = len(series) == 1

    for s in series:
        series_attr = f' data-series="{_esc(s["slug"])}"' if s.get("slug") else ""

        pts = [(x_of(i), y_of(v)) for i, v in enumerate(s["values"]) if v is not None]
        if len(pts) >= 2:
            path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
            parts.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" stroke-width="2" '
                         f'stroke-linecap="round" stroke-linejoin="round"{series_attr} />')

        last_i = None
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            last_i = i
            cx, cy = x_of(i), y_of(v)
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{s["color"]}" '
                f'stroke="{SURFACE}" stroke-width="2"{series_attr}>'
                f'<title>{_esc(categories[i])} - {s["name"]}: {v:g}{unit}</title></circle>'
            )
        if label_endpoints and last_i is not None:
            cx, cy = x_of(last_i), y_of(s["values"][last_i])
            label = f'{s["values"][last_i]:g}{unit}'
            if cx <= width - PAD_RIGHT - 45:
                # Room to the right of the marker - the clearest spot, clear of the line.
                lx, ly, anchor = cx + 10, cy + 4, "start"
            else:
                # Last point sits at the right edge (the common case, since the x-axis
                # usually ends there) - putting the label beside it would run off the
                # chart, and centering it above always would sometimes sit right on top
                # of the incoming line segment. Prefer whichever side the line ISN'T
                # approaching from.
                prev_v = next((v for v in reversed(s["values"][:last_i]) if v is not None), None)
                below = prev_v is not None and prev_v < s["values"][last_i]
                lx = cx
                ly = cy + 18 if below else cy - 12
                anchor = "middle"
            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
                f'class="end-label" fill="{s["color"]}">{_esc(label)}</text>'
            )

    parts.append("</svg>")

    if len(series) > 1:
        legend = ['<div class="chart-legend">']
        for s in series:
            series_attr = f' data-series="{_esc(s["slug"])}"' if s.get("slug") else ""
            legend.append(
                f'<span class="legend-item"{series_attr}><i style="background:{s["color"]}"></i>{_esc(s["name"])}</span>'
            )
        legend.append("</div>")
        parts.append("".join(legend))

    return Markup("".join(parts))


def grouped_bar_chart(categories, series, width=680, height=280, unit="", y_max=None, bar_max=22, integer_y=True):
    """
    categories: x-axis groups (e.g. levels).
    series: list of {"name", "color", "values"} aligned to categories; None = no bar.
    integer_y: whole-number y-axis gridlines only (the default - bar
        charts here are almost always counts, e.g. "3 picks", and a 0.1
        gridline for that is never meaningful). Pass False for a bar
        chart of continuous values.
    """
    all_vals = [v for s in series for v in s["values"] if v is not None]
    if not all_vals:
        return Markup('<p class="chart-empty">No data yet.</p>')

    hi = y_max if y_max is not None else max(all_vals) * 1.12
    lo = 0

    plot_w = width - PAD_LEFT - PAD_RIGHT
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = len(categories)
    group_w = plot_w / max(n, 1)
    n_series = len(series)
    bar_w = min(bar_max, (group_w * 0.72) / max(n_series, 1))
    gap = 2

    def y_of(v):
        return PAD_TOP + plot_h - (v - lo) / (hi - lo) * plot_h

    ticks = _nice_ticks(lo, hi, integer=integer_y)
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="Bar chart">'
    ]

    for t in ticks:
        y = y_of(t)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{width - PAD_RIGHT}" y2="{y:.1f}" class="grid-line" />'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 8}" y="{y:.1f}" class="axis-label" '
            f'text-anchor="end" dominant-baseline="middle">{t:g}{unit}</text>'
        )

    baseline_y = y_of(0)
    group_total_w = n_series * bar_w + (n_series - 1) * gap

    for gi, cat in enumerate(categories):
        group_left = PAD_LEFT + group_w * gi + (group_w - group_total_w) / 2

        for si, s in enumerate(series):
            v = s["values"][gi]
            if v is None:
                continue
            bx = group_left + si * (bar_w + gap)
            by = y_of(v)
            bh = baseline_y - by
            r = min(4, bh) if bh > 0 else 0
            parts.append(
                f'<path d="M{bx:.1f},{baseline_y:.1f} L{bx:.1f},{by + r:.1f} '
                f'Q{bx:.1f},{by:.1f} {bx + r:.1f},{by:.1f} '
                f'L{bx + bar_w - r:.1f},{by:.1f} '
                f'Q{bx + bar_w:.1f},{by:.1f} {bx + bar_w:.1f},{by + r:.1f} '
                f'L{bx + bar_w:.1f},{baseline_y:.1f} Z" fill="{s["color"]}">'
                f'<title>{_esc(cat)} - {s["name"]}: {v:g}{unit}</title></path>'
            )

        # one centered label per group, not one per bar
        parts.append(
            f'<text x="{group_left + group_total_w / 2:.1f}" y="{height - PAD_BOTTOM + 18}" '
            f'class="axis-label" text-anchor="middle">{_esc(cat)}</text>'
        )

    parts.append("</svg>")

    if n_series > 1:
        legend = ['<div class="chart-legend">']
        for s in series:
            legend.append(
                f'<span class="legend-item"><i style="background:{s["color"]}"></i>{_esc(s["name"])}</span>'
            )
        legend.append("</div>")
        parts.append("".join(legend))

    return Markup("".join(parts))
