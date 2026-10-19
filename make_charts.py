"""
Draws the two charts used in the README, as SVG files, with no extra packages.

  results/backtest_returns.svg - 30-day backtest return of each strategy vs. hodl
  results/live_equity.svg      - wallet value over time during a live paper run

Run it with:   python make_charts.py
Inputs:        results/backtest_report_30d.txt   (saved output of backtest.py)
               results/equity_sample.csv         (a copy of a run's equity.csv)
"""

import csv
import os
import re
from datetime import datetime
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")

HODL = "#2563eb"      # the benchmark: buy and hold
UNSAFE = "#dc2626"    # new-coin strategies that skip the safety checks
OTHER = "#9ca3af"
INK = "#111827"
MUTED = "#6b7280"
FONT = 'font-family="Segoe UI, Helvetica, Arial, sans-serif"'


def svg(width, height, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" {FONT}>\n'
            f'<rect width="100%" height="100%" fill="#ffffff"/>\n{body}\n</svg>\n')


def text(x, y, s, size=12, color=INK, anchor="start", weight="normal"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>')


def backtest_chart():
    """Horizontal bars: each strategy's return over the 30-day backtest."""
    rows = []
    with open(os.path.join(RESULTS, "backtest_report_30d.txt"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^(\w+)\s+\$\s*[\d,.]+\s+([+-][\d.]+)%", line)
            if m:
                rows.append((m.group(1), float(m.group(2))))

    width, left, right, top, bar_h, gap = 760, 150, 70, 70, 26, 10
    height = top + len(rows) * (bar_h + gap) + 50
    lo, hi = min(-100.0, min(r for _, r in rows)), max(10.0, max(r for _, r in rows))
    x = lambda v: left + (v - lo) / (hi - lo) * (width - left - right)

    out = [text(20, 30, "30-day backtest: every strategy lost money; none beat holding "
                "in both halves", 16, weight="bold"),
           text(20, 50, "Return after real trading costs, $1,000 per strategy, 6 established "
                "Solana memecoins", 12, MUTED)]
    for tick in range(int(lo), int(hi) + 1, 20):
        out.append(f'<line x1="{x(tick):.1f}" y1="{top - 6}" x2="{x(tick):.1f}" '
                   f'y2="{height - 40}" stroke="#e5e7eb"/>')
        out.append(text(x(tick), height - 24, f"{tick:+d}%" if tick else "0%", 11, MUTED,
                        "middle"))
    for i, (name, ret) in enumerate(rows):
        y = top + i * (bar_h + gap)
        color = HODL if name == "hodl" else OTHER
        x0, x1 = sorted((x(0), x(ret)))
        out.append(f'<rect x="{x0:.1f}" y="{y}" width="{max(x1 - x0, 1):.1f}" '
                   f'height="{bar_h}" fill="{color}" rx="2"/>')
        label = name + ("  (benchmark)" if name == "hodl" else "")
        out.append(text(left - 8, y + bar_h / 2 + 4, label, 12, INK, "end",
                        "bold" if name == "hodl" else "normal"))
        out.append(text(x(0) + 8, y + bar_h / 2 + 4, f"{ret:+.1f}%", 12, INK))
    out.append(f'<line x1="{x(0):.1f}" y1="{top - 6}" x2="{x(0):.1f}" y2="{height - 40}" '
               f'stroke="{INK}"/>')
    return svg(width, height, "\n".join(out))


def equity_chart():
    """One line per strategy: wallet value over a live paper-trading run."""
    series = {}
    with open(os.path.join(RESULTS, "equity_sample.csv"), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            series.setdefault(row["strategy"], []).append((t, float(row["value"])))

    width, height, left, right, top, bottom = 760, 460, 60, 170, 70, 40
    t0 = min(p[0] for s in series.values() for p in s)
    t1 = max(p[0] for s in series.values() for p in s)
    span = max((t1 - t0).total_seconds(), 1)
    lo, hi = 0.0, 1100.0
    x = lambda t: left + (t - t0).total_seconds() / span * (width - left - right)
    y = lambda v: top + (hi - v) / (hi - lo) * (height - top - bottom)

    def color(name):
        return HODL if name == "hodl" else UNSAFE if "unsafe" in name else OTHER

    hours = span / 3600
    out = [text(20, 30, f"Live paper trading, first {hours:.0f} hours: skipping safety "
                "checks was the costliest mistake", 16, weight="bold"),
           text(20, 50, "Wallet value per strategy (each starts at $1,000). Blue = hodl "
                "benchmark, red = no safety checks.", 12, MUTED)]
    for v in range(0, 1101, 250):
        out.append(f'<line x1="{left}" y1="{y(v):.1f}" x2="{width - right}" y2="{y(v):.1f}" '
                   f'stroke="#e5e7eb"/>')
        out.append(text(left - 6, y(v) + 4, f"${v:,}", 11, MUTED, "end"))
    out.append(text(left, height - 14, t0.strftime("%Y-%m-%d %H:%M"), 11, MUTED))
    out.append(text(width - right, height - 14, t1.strftime("%H:%M"), 11, MUTED, "end"))

    # Grey lines first so the highlighted ones sit on top.
    order = sorted(series, key=lambda n: color(n) != OTHER)
    for name in order:
        pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in series[name])
        w = 1.2 if color(name) == OTHER else 2.5
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color(name)}" '
                   f'stroke-width="{w}"/>')

    # End labels, nudged apart so they don't overlap.
    ends = sorted(((series[n][-1][1], n) for n in series), reverse=True)
    last_y = -1e9
    for value, name in ends:
        ly = max(y(value) + 4, last_y + 13)
        last_y = ly
        out.append(text(width - right + 8, ly, f"{name} ${value:,.0f}", 11, color(name)
                        if color(name) != OTHER else MUTED))
    return svg(width, height, "\n".join(out))


def main():
    for name, make in (("backtest_returns.svg", backtest_chart),
                       ("live_equity.svg", equity_chart)):
        path = os.path.join(RESULTS, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(make())
        print("Wrote", os.path.relpath(path, HERE))


if __name__ == "__main__":
    main()
