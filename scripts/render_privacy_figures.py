#!/usr/bin/env python3
# Copyright (c) 2026 Perry Kundert.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the Privacy paper's narrow, cumulative flow diagrams (PNG and SVG).

Run from any directory: python3 scripts/render_privacy_figures.py
Requires matplotlib. Figure content lives in doc/privacy/figures.json.
The SVGs retain editable text; PNGs work in Org, HTML and ordinary LaTeX.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle, Arc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "images/privacy"
INK, MUTED, TEAL, AMBER = "#162c3b", "#596c79", "#006c67", "#9a520c"
matplotlib.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})


def draw(item):
    fig, ax = plt.subplots(figsize=(12, 2.6), dpi=200)
    fig.subplots_adjust(0, 0, 1, 1)
    ax.set(xlim=(0, 1200), ylim=(260, 0))
    ax.axis("off")
    text_bounds = []

    def label(x, y, text, size=18, color=INK, weight="normal", width=None, **kw):
        artist = ax.text(x, y, text, fontsize=size, color=color, fontweight=weight,
                         va="center", **kw)
        if width:
            text_bounds.append((artist, width))
        return artist

    def box(x, y, w, h, face, edge, lw=1):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=6",
                                   facecolor=face, edgecolor=edge, linewidth=lw))

    for x, name, key, tint, color in [
        (8, "BOB", "bob", "#eef6f5", TEAL),
        (420, "MALLORY", "mallory", "#fff7eb", AMBER),
        (832, "CAROL", "carol", "#eef6f5", TEAL),
    ]:
        label(x + 4, 16, name, weight="bold", color=color)
        if key == "mallory":
            label(x + 162, 16, "OUTSIDE OBSERVER", size=11.5, color=AMBER)
        box(x, 35, 360, 131, tint, "#cad7dc")
        card = item[key]
        label(x + 12, 51, card[0], size=12, color=color, weight="bold", width=334)
        for n, (state, words) in enumerate(card[1:]):
            yy = 77 + n * 31
            fresh = state in ("new", "lock", "check", "open", "stop")
            if fresh:
                box(x + 7, yy - 13, 346, 28, "#ffffff", "#dae5e5", .7)
            ic = AMBER if state == "stop" or key == "mallory" else TEAL
            if state == "lock":
                ax.add_patch(Arc((x + 22, yy - 4), 11, 13, theta1=180, theta2=360,
                                 color=ic, linewidth=1.25))
                ax.add_patch(Rectangle((x + 15, yy - 4), 14, 12, color=ic))
            else:
                glyph = {"new": "+", "check": "✓", "open": "↳", "stop": "×",
                         "keep": "·", "see": "·"}[state]
                label(x + 22, yy, glyph, size=17, color=ic, weight="bold", ha="center")
            label(x + 39, yy, words, size=16.5,
                  color=INK if fresh else MUTED,
                  weight="bold" if state == "new" else "normal", width=307)

    route = item.get("route", "local")
    arrows = {
        "bc": [(38, 186, 1162, 186)], "cb": [(1162, 186, 38, 186)],
        "down_b": [(26, 166, 26, 214)], "down_c": [(1174, 166, 1174, 214)],
        "up_b": [(26, 214, 26, 166)], "up_c": [(1174, 214, 1174, 166)],
        "up_both": [(26, 214, 26, 166), (1174, 214, 1174, 166)],
        "down_both": [(26, 166, 26, 214), (1174, 166, 1174, 214)],
        "local": [],
    }
    for x1, y1, x2, y2 in arrows[route]:
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                                    mutation_scale=12, linewidth=1.25, color=TEAL))
    label(600, 185, item["flow"], size=14, ha="center", width=1095,
          bbox=dict(facecolor="white", edgecolor="none", pad=2))
    box(8, 207, 1184, 48, "#edf1f5", "#c9d2db")
    label(24, 231, item["service"][0], size=12, weight="bold", color=INK, width=240)
    ax.plot([273, 273], [215, 247], color="#c9d2db", linewidth=1)
    label(289, 222, item["service"][1], size=14.5, width=881)
    label(289, 242, item["service"][2], size=14.5, color=MUTED, width=881)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    scale = ax.transData.transform((1, 0))[0] - ax.transData.transform((0, 0))[0]
    for artist, available in text_bounds:
        actual = artist.get_window_extent(renderer).width / scale
        if actual > available and actual <= available * 1.08:
            artist.set_fontsize(artist.get_fontsize() * (available - 2) / actual)
        elif actual > available:
            raise ValueError(f"{item['id']}: label exceeds its box ({actual:.0f}>{available}): "
                             f"{artist.get_text()}")
    alt = " | ".join([item["flow"]] + [
        f"{name.title()}: " + "; ".join(row[1] for row in item[name][1:])
        for name in ("bob", "mallory", "carol")
    ] + item["service"])
    for ext in ("png", "svg"):
        metadata = ({"Description": alt} if ext == "png" else
                    {"Description": alt, "Date": "2026-09-24",
                     "Rights": "Copyright 2026 Perry Kundert. All rights reserved."})
        fig.savefig(OUT / f"{item['id']}.{ext}", facecolor="white", metadata=metadata)
    plt.close(fig)


def main():
    source = json.loads((ROOT / "doc/privacy/figures.json").read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    figures = source["figures"]
    ids = [item["id"] for item in figures]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate figure ID")
    for item in figures:
        draw(item)
    print(f"Rendered {len(figures)} figures as PNG + editable SVG in {OUT}")


if __name__ == "__main__":
    main()
