#!/usr/bin/env python3
"""Render the PnL bucket donut as SVG with exact geometry, straight from DuckDB.

An image model cannot compute arc angles. This can. Transparent background so
the result can be composited onto any artwork.

    python make_chart.py [--realized] [-o chart.svg]
"""
import argparse
import math
from xml.sax.saxutils import escape

import store

W, H = 1536, 1024
CX, CY = 700, 545
R_OUT, R_IN = 250, 140
GREENS = ["#0aa63c", "#17c44e", "#3ad964", "#6ee88a", "#a6f2b6", "#d6fadd"]
REDS = ["#f6d8d8", "#f3b4b4", "#ec8080", "#e05252", "#cf2b2b", "#a11414"]


def polar(cx, cy, r, deg):
    a = math.radians(deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def ring_path(a0, a1):
    x0, y0 = polar(CX, CY, R_OUT, a0)
    x1, y1 = polar(CX, CY, R_OUT, a1)
    x2, y2 = polar(CX, CY, R_IN, a1)
    x3, y3 = polar(CX, CY, R_IN, a0)
    big = 1 if (a1 - a0) > 180 else 0
    return (f"M{x0:.2f},{y0:.2f} A{R_OUT},{R_OUT} 0 {big} 1 {x1:.2f},{y1:.2f} "
            f"L{x2:.2f},{y2:.2f} A{R_IN},{R_IN} 0 {big} 0 {x3:.2f},{y3:.2f} Z")


def layout(labels, top, bottom, gap=64):
    """Push overlapping callouts apart while keeping their order."""
    labels.sort(key=lambda l: l["y"])
    for i in range(1, len(labels)):
        if labels[i]["y"] - labels[i - 1]["y"] < gap:
            labels[i]["y"] = labels[i - 1]["y"] + gap
    overflow = labels[-1]["y"] - bottom if labels else 0
    if overflow > 0:
        for l in labels:
            l["y"] -= overflow
    if labels and labels[0]["y"] < top:
        shift = top - labels[0]["y"]
        for l in labels:
            l["y"] += shift
    return labels


def build(rows, total, title, subtitle, bg=False):
    # clockwise from 12: gains descending, then losses descending
    gains = [r for r in rows if r["lo"] >= 0][::-1]
    losses = [r for r in rows if r["lo"] < 0]
    ordered = gains + losses[::-1]

    for i, r in enumerate(gains):
        r["fill"] = GREENS[min(i, len(GREENS) - 1)]
    for i, r in enumerate(losses[::-1]):
        r["fill"] = REDS[min(i, len(REDS) - 1)]

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}" font-family="Inter,Helvetica,Arial,sans-serif">']
    svg.append('<defs><filter id="g" x="-50%" y="-50%" width="200%" height="200%">'
               '<feGaussianBlur stdDeviation="7" result="b"/>'
               '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
               '</filter></defs>')
    if bg:
        svg.append(f'<rect width="{W}" height="{H}" fill="#05070d"/>')
    svg.append(f'<text x="90" y="96" fill="#fff" font-size="62" font-weight="800" '
               f'filter="url(#g)">{title}</text>')
    svg.append(f'<text x="92" y="148" fill="#dbeafe" font-size="30" font-weight="700" '
               f'letter-spacing="2.5">{subtitle}</text>')

    ang = 0.0
    left, right = [], []
    for r in ordered:
        sweep = r["pct"] / 100.0 * 360.0
        a0, a1 = ang + 0.35, ang + sweep - 0.35
        if a1 <= a0:                      # sub-degree slice: keep it visible
            a0, a1 = ang, ang + max(sweep, 0.55)
        svg.append(f'<path d="{ring_path(a0, a1)}" fill="{r["fill"]}" '
                   f'stroke="#05070d" stroke-width="2"/>')
        mid = ang + sweep / 2
        px, py = polar(CX, CY, (R_OUT + R_IN) / 2, mid)
        side = "r" if mid < 180 else "l"
        (right if side == "r" else left).append(
            {"y": py, "px": px, "py": py, "row": r, "side": side})
        ang += sweep

    for group, x_anchor, x_text, align in (
        (layout(left, 190, 900), 430, 400, "end"),
        (layout(right, 190, 900), 1000, 1030, "start"),
    ):
        for l in group:
            r, y = l["row"], l["y"]
            c = "#ff5a5a" if r["lo"] < 0 else "#3ad964"
            svg.append(f'<polyline points="{l["px"]:.1f},{l["py"]:.1f} '
                       f'{x_anchor},{y:.1f} {x_text},{y:.1f}" fill="none" '
                       f'stroke="{r["fill"]}" stroke-width="1.6" opacity="0.85"/>')
            svg.append(f'<circle cx="{l["px"]:.1f}" cy="{l["py"]:.1f}" r="4.5" fill="{r["fill"]}"/>')
            dx = -14 if align == "end" else 14
            svg.append(f'<text x="{x_text + dx}" y="{y - 6:.1f}" text-anchor="{align}" '
                       f'fill="#fff" font-size="27" font-weight="800">{r["traders"]:,}'
                       f'<tspan fill="{c}" dx="14">{r["pct"]:.2f}%</tspan></text>')
            svg.append(f'<text x="{x_text + dx}" y="{y + 24:.1f}" text-anchor="{align}" '
                       f'fill="#9fd0ff" font-size="23" font-weight="600">{escape(r["bucket"])}</text>')

    svg.append(f'<circle cx="{CX}" cy="{CY}" r="{R_IN - 6}" fill="#05070d" opacity="0.92"/>')
    svg.append(f'<text x="{CX}" y="{CY + 6}" text-anchor="middle" fill="#fff" '
               f'font-size="62" font-weight="800" filter="url(#g)">{total:,}</text>')
    svg.append(f'<text x="{CX}" y="{CY + 44}" text-anchor="middle" fill="#cbd5e1" '
               f'font-size="20" font-weight="600" letter-spacing="4">TOTAL TRADERS</text>')
    svg.append("</svg>")
    return "\n".join(svg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--realized", action="store_true")
    ap.add_argument("-o", "--out", default="chart.svg")
    ap.add_argument("--bg", action="store_true", help="paint a dark backdrop (preview/standalone)")
    args = ap.parse_args()

    tbl = "realized_buckets" if args.realized else "pnl_buckets"
    con = store.connect(read_only=True)
    rows = con.execute(f"SELECT ord, bucket, traders FROM {tbl} ORDER BY ord").fetchall()
    con.close()

    total = sum(r[2] for r in rows)
    import config
    data = [{"bucket": b, "traders": t, "pct": t / total * 100,
             "lo": config.PNL_BUCKETS[o][1]} for o, b, t in rows]

    svg = build(data, total,
                "fomo", "THE LATEST uPNL PONZI" if not args.realized
                else "REALIZED PNL, CLOSED TRADES ONLY", bg=args.bg)
    with open(args.out, "w") as f:
        f.write(svg)
    print(f"wrote {args.out}  ({total:,} traders, {len(data)} segments)")
    for d in data:
        print(f"  {d['bucket']:>16}  {d['traders']:>6,}  {d['pct']:5.2f}%  "
              f"{d['pct']/100*360:6.2f}deg")


if __name__ == "__main__":
    main()
