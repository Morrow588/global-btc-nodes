#!/usr/bin/env python3
"""Generate the README assets (banner + showcase chart) from the real dataset.

Every number drawn here is read from the versioned files in `data/`; nothing is
invented or hard-coded, so re-running this script after the daily pipeline
commits new data regenerates the same pictures with the current numbers.

Usage
-----
    python -m venv .venv-assets && source .venv-assets/bin/activate
    pip install matplotlib
    python docs/assets/make_assets.py

matplotlib is an *asset* dependency only: it is not imported by any runtime
code in this repository, so it deliberately does not appear in
requirements.txt (which stays small enough for a clean clone of the pipeline).

Outputs (written next to this file):
    docs/assets/banner.png   1280x640  hero banner
    docs/assets/showcase.png 1280x760  real BTC-USD history + node telemetry
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from datetime import datetime  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "btc_market_daily.csv"
JSON_PATH = ROOT / "data" / "btc_node_metrics.json"

BG = "#0d1117"
FG = "#c9d1d9"
MUTED = "#8b949e"
GRID = "#21262d"
ORANGE = "#f7931a"
BLUE = "#58a6ff"


def load_candles() -> list[tuple[datetime, float, float]]:
    """Return (date, close, volume) for every closed daily candle."""
    out: list[tuple[datetime, float, float]] = []
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                (
                    datetime.strptime(row["date"], "%Y-%m-%d"),
                    float(row["close"]),
                    float(row["volume"]),
                )
            )
    out.sort(key=lambda r: r[0])
    return out


def load_snapshots() -> list[dict]:
    doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    snaps = doc.get("snapshots", [])
    snaps.sort(key=lambda s: s["date"])
    return snaps


def usd(value: float) -> str:
    return f"${value:,.0f}"


def make_banner(candles: list[tuple[datetime, float, float]]) -> None:
    fig = plt.figure(figsize=(12.8, 6.4), dpi=100, facecolor=BG)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    # A dim, real sparkline of the dataset sits behind the text block.
    ax = fig.add_axes([0.0, 0.0, 1.0, 0.26], facecolor=BG)
    dates = [c[0] for c in candles]
    closes = [c[1] for c in candles]
    ax.fill_between(dates, closes, min(closes), color=ORANGE, alpha=0.16, linewidth=0)
    ax.plot(dates, closes, color=ORANGE, linewidth=1.6, alpha=0.85)
    ax.set_ylim(min(closes) - 4000, max(closes) + 2000)
    ax.set_xlim(dates[0], dates[-1])
    ax.axis("off")

    first, last = candles[0], candles[-1]
    span = f"{len(candles)} closed daily BTC-USD candles  ·  {first[0]:%Y-%m-%d} to {last[0]:%Y-%m-%d}"

    fig.text(0.06, 0.86, "virtualtradrs / global-btc-nodes", color=MUTED, fontsize=15, family="monospace")
    fig.text(
        0.06,
        0.70,
        "Bitcoin Node & Market Data Toolkit",
        color="#ffffff",
        fontsize=38,
        fontweight="bold",
    )
    fig.text(
        0.06,
        0.60,
        "for AI Algorithmic Trading",
        color=ORANGE,
        fontsize=38,
        fontweight="bold",
    )
    fig.text(
        0.06,
        0.47,
        "Live Bitcoin network & market data for reproducible algorithmic trading research",
        color=FG,
        fontsize=17,
    )
    fig.text(0.06, 0.40, span, color=MUTED, fontsize=13)
    fig.text(
        0.06,
        0.33,
        "Scheduled pipeline  ·  CI-validated  ·  versioned in-repo dataset  ·  MIT licensed",
        color=MUTED,
        fontsize=13,
    )
    fig.text(0.06, 0.27, f"Latest close {usd(last[1])} on {last[0]:%Y-%m-%d}", color=ORANGE, fontsize=13)
    fig.text(0.94, 0.27, "github.com/virtualtradrs/global-btc-nodes", color=MUTED, fontsize=12, ha="right")

    fig.savefig(ASSETS / "banner.png", facecolor=BG)
    plt.close(fig)


def make_showcase(candles: list[tuple[datetime, float, float]], snaps: list[dict]) -> None:
    fig = plt.figure(figsize=(12.8, 7.6), dpi=100, facecolor=BG)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.32, left=0.075, right=0.955, top=0.90, bottom=0.09)

    dates = [c[0] for c in candles]
    closes = [c[1] for c in candles]

    def style(ax) -> None:
        ax.set_facecolor(BG)
        for side in ax.spines.values():
            side.set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=10)
        ax.grid(color=GRID, linewidth=0.7, alpha=0.9)
        ax.set_axisbelow(True)

    # --- Panel 1: real BTC-USD daily close history -------------------------
    ax1 = fig.add_subplot(gs[0])
    style(ax1)
    ax1.plot(dates, closes, color=ORANGE, linewidth=1.8)
    ax1.fill_between(dates, closes, min(closes), color=ORANGE, alpha=0.10, linewidth=0)

    hi = max(candles, key=lambda c: c[1])
    lo = min(candles, key=lambda c: c[1])
    last = candles[-1]
    for point, label, va in ((hi, f"high {usd(hi[1])}  {hi[0]:%Y-%m-%d}", "bottom"), (lo, f"low {usd(lo[1])}  {lo[0]:%Y-%m-%d}", "top")):
        ax1.scatter([point[0]], [point[1]], s=22, color="#ffffff", zorder=5)
        ax1.annotate(
            label,
            xy=(point[0], point[1]),
            xytext=(0, 10 if va == "bottom" else -18),
            textcoords="offset points",
            color=FG,
            fontsize=10,
            ha="center",
        )
    ax1.scatter([last[0]], [last[1]], s=26, color=BLUE, zorder=5)
    ax1.annotate(
        f"latest {usd(last[1])}  {last[0]:%Y-%m-%d}",
        xy=(last[0], last[1]),
        xytext=(-10, 14),
        textcoords="offset points",
        color=BLUE,
        fontsize=10,
        ha="right",
    )
    ax1.set_ylim(min(closes) * 0.9, max(closes) * 1.09)
    ax1.set_xlim(dates[0], dates[-1])
    ax1.yaxis.set_major_formatter(lambda v, _: usd(v))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax1.set_title(
        f"BTC-USD daily close, {dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d}  ({len(candles)} closed candles)",
        color=FG,
        fontsize=13,
        loc="left",
    )
    ax1.set_ylabel("USD (close)", color=MUTED, fontsize=10)

    # --- Panel 2: the node / mempool telemetry actually in the repo --------
    ax2 = fig.add_subplot(gs[1])
    style(ax2)
    sdates = [datetime.strptime(s["date"], "%Y-%m-%d") for s in snaps]
    mempool = [s["chain"]["mempool_size_mb"] for s in snaps]
    reach = [s["node_network"]["reachable_nodes"] for s in snaps]
    ax2.bar(sdates, mempool, width=0.18, color=ORANGE, alpha=0.55, label="mempool size (MB)")
    ax2.set_ylim(0, max(mempool) * 1.6)
    ax2.set_xlim(mdates.date2num(sdates[0]) - 0.5, mdates.date2num(sdates[-1]) + 0.5)
    ax2.set_ylabel("mempool MB", color=MUTED, fontsize=9)
    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=1))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax2.set_title(
        "Bitcoin network telemetry (data/btc_node_metrics.json) — snapshots currently retained in the dataset: "
        + ", ".join(f"{s['date']} ({s['chain']['tip_height']:,})" for s in snaps),
        color=FG,
        fontsize=10.5,
        loc="left",
    )
    ax2b = ax2.twinx()
    ax2b.plot(sdates, reach, color=BLUE, marker="o", linewidth=1.8, label="reachable nodes")
    ax2b.set_ylim(min(reach) * 0.95, max(reach) * 1.05)
    ax2b.tick_params(colors=MUTED, labelsize=9)
    for value, x in zip(reach, sdates):
        ax2b.annotate(f"{value:,} nodes", xy=(x, value), xytext=(0, 8), textcoords="offset points", color=BLUE, fontsize=9, ha="center")
    for label in ax2b.get_yticklabels():
        label.set_color(BLUE)

    fig.text(0.075, 0.965, "Reproducible research on the versioned dataset", color="#ffffff", fontsize=17, fontweight="bold")
    fig.text(
        0.075,
        0.935,
        "both panels are drawn straight from data/ committed by the daily GitHub Actions pipeline",
        color=MUTED,
        fontsize=10.5,
    )
    fig.savefig(ASSETS / "showcase.png", facecolor=BG)
    plt.close(fig)


def main() -> int:
    candles = load_candles()
    snaps = load_snapshots()
    make_banner(candles)
    make_showcase(candles, snaps)
    print(f"banner.png + showcase.png written from {len(candles)} candles and {len(snaps)} snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
