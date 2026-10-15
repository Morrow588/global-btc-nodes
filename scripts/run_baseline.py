#!/usr/bin/env python3
"""Baseline experiment: long-only SMA crossover vs buy-and-hold.

Runs a classic fast/slow simple-moving-average crossover on the daily closes in
``data/btc_market_daily.csv`` and reports it next to buy-and-hold over the *same*
period, net of the same per-trade costs. It exists so that any later model has a
measured floor to beat instead of a claim, and so that the numbers in the README
come from running code on the versioned dataset rather than from a spreadsheet.

Strategy
--------
Long-only. Flat unless the fast SMA is strictly above the slow SMA (defaults
20/100, which fits the 721 committed candles: the slow window still gets ~620
trading days of live signals). No shorting, no leverage, one position at a time,
whole capital committed per entry (BTC is divisible, so fractional units are used
and there is no rounding-to-lot error).

Signal timing (no look-ahead)
-----------------------------
Signals are computed from closes up to and including day ``t``. The resulting
position applies from the NEXT trading day ``t+1``: the order is filled at the
CLOSE of ``t+1``, so nothing in the equity curve is decided using a price that
was not yet observable at the time of the decision. A signal at ``t`` can never
change the return earned over ``t-1 -> t``.

Warm-up
-------
The backtest starts at row ``max(slow_window, warmup_rows)`` of the dataset (1-based),
i.e. the first row whose slow SMA is fully defined. Rows before that are only used
to seed the moving averages and never earn a return, so the warm-up cannot leak
future information into the equity curve. Buy-and-hold is measured over the same
window, with the same costs, so the comparison is like-for-like.

Costs
-----
``fee_pct_per_side`` + ``slippage_pct_per_side`` (defaults 0.10% + 0.05%) are
charged on the executed notional of every fill, on both the entry and the exit.
Costs are parameters of the experiment and are recorded in the output next to the
results. A position still open at the end of the window is marked to market at the
final close and has *not* paid a hypothetical exit cost; that rule is applied to
both strategies so the comparison stays honest.

Determinism
-----------
Standard library only (``csv``/``json``/``math``) - no numpy/pandas, no RNG, no
network, no clock in the numbers. Same dataset in, byte-identical results out,
which is what makes ``--check`` meaningful in CI.

Usage
-----
    python scripts/run_baseline.py                  # write results/baseline_results.json
    python scripts/run_baseline.py --check          # verify the committed numbers still hold
    python scripts/run_baseline.py --chart          # also draw results/baseline_equity.png (needs matplotlib)
    python scripts/run_baseline.py --print-only     # compute and print, write nothing

matplotlib is an *asset* dependency only: it is imported lazily by ``--chart`` and
is deliberately not in requirements.txt, exactly like docs/assets/make_assets.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

SCRIPT_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "data" / "btc_market_daily.csv"
RESULTS_PATH = ROOT / "results" / "baseline_results.json"
EQUITY_PNG_PATH = ROOT / "results" / "baseline_equity.png"

# ---------------------------------------------------------------------------
# Config block: every number the experiment depends on lives here.
# ---------------------------------------------------------------------------
CONFIG: dict[str, object] = {
    "strategy": "long-only SMA crossover (fast > slow), flat otherwise",
    "fast_window": 20,
    "slow_window": 100,
    # Rows used to warm the moving averages before any return is earned. The
    # effective start is max(slow_window, warmup_rows) - see the docstring.
    "warmup_rows": 100,
    "fee_pct_per_side": 0.10,
    "slippage_pct_per_side": 0.05,
    "start_capital_usd": 10_000.00,
}

BASELINE_KEY = "sma_crossover_{fast_window}_{slow_window}".format(**CONFIG)
BUY_HOLD_KEY = "buy_and_hold"

# Metric keys every strategy entry must carry; check_repo.py enforces this too.
METRIC_KEYS = [
    "total_return_pct",
    "cagr_pct",
    "max_drawdown_pct",
    "num_trades",
    "win_rate_pct",
    "pct_time_in_market",
    "final_equity",
]


def round2(value: float | None) -> float | None:
    """Round for reporting. Rule recorded in the output as 'rounding'."""
    if value is None:
        return None
    return round(value, 2)


def load_candles(path: Path) -> tuple[list[str], list[float]]:
    """Read the versioned daily candles. Dates are ISO strings (lexicographically sortable)."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path}: no data rows")
    dates = [row["date"] for row in rows]
    closes = [float(row["close"]) for row in rows]
    if dates != sorted(dates):
        raise SystemExit(f"{path}: rows are not sorted by date")
    return dates, closes


def sma_series(closes: list[float], window: int) -> list[float | None]:
    """Simple moving average; ``None`` until ``window`` closes are available.

    math.fsum keeps the running sums free of accumulation drift, so the script
    produces identical output on any platform/Python build.
    """
    out: list[float | None] = [None] * len(closes)
    for i in range(window - 1, len(closes)):
        out[i] = math.fsum(closes[i - window + 1 : i + 1]) / window
    return out


def max_drawdown_pct(equity: list[float]) -> float:
    """Largest peak-to-trough decline of the equity curve, as a positive percentage."""
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def cagr_pct(start_equity: float, end_equity: float, calendar_days: int) -> float:
    """Annualised return over the *calendar* length of the window (365.2425-day year)."""
    if calendar_days <= 0:
        return 0.0
    years = calendar_days / 365.2425
    if start_equity <= 0:
        return 0.0
    return ((end_equity / start_equity) ** (1.0 / years) - 1.0) * 100.0


def summarise(
    equity: list[float],
    in_market: list[bool],
    start_capital: float,
    calendar_days: int,
    num_trades: int,
    round_trip_pnls: list[float],
) -> dict[str, float | int | None]:
    """Common metrics for both strategies, so they are computed by the same code."""
    final_equity = equity[-1]
    win_rate: float | None = None
    if round_trip_pnls:
        win_rate = 100.0 * sum(1 for pnl in round_trip_pnls if pnl > 0) / len(round_trip_pnls)
    return {
        "total_return_pct": round2((final_equity / start_capital - 1.0) * 100.0),
        "cagr_pct": round2(cagr_pct(start_capital, final_equity, calendar_days)),
        "max_drawdown_pct": round2(max_drawdown_pct(equity)),
        "num_trades": num_trades,
        "win_rate_pct": round2(win_rate),
        "pct_time_in_market": round2(100.0 * sum(in_market) / len(in_market)),
        "final_equity": round2(final_equity),
    }


def run_sma_crossover(
    dates: list[str], closes: list[float], start_index: int, cost_rate: float, capital: float
) -> dict[str, object]:
    """Long-only crossover. Returns metrics, the equity curve and diagnostics."""
    fast = sma_series(closes, int(CONFIG["fast_window"]))
    slow = sma_series(closes, int(CONFIG["slow_window"]))

    cash = capital
    units = 0.0
    position = 0
    entry_equity: float | None = None
    equity: list[float] = []
    in_market: list[bool] = []
    trades = 0
    entries = 0
    exits = 0
    round_trip_pnls: list[float] = []

    for j in range(start_index, len(closes)):
        if j > start_index:
            # Signal known at the close of j-1; the fill happens at the close of j.
            desired = 1 if fast[j - 1] > slow[j - 1] else 0  # type: ignore[operator]
            if desired != position:
                close = closes[j]
                if desired == 1:
                    notional = cash
                    cash = 0.0
                    units = (notional - notional * cost_rate) / close
                    entry_equity = notional - notional * cost_rate
                    position = 1
                    entries += 1
                else:
                    notional = units * close
                    cash = notional - notional * cost_rate
                    units = 0.0
                    position = 0
                    exits += 1
                    assert entry_equity is not None
                    round_trip_pnls.append(cash / entry_equity - 1.0)
                    entry_equity = None
                trades += 1
        equity.append(cash + units * closes[j])
        in_market.append(position == 1)

    # A position still open at the end is counted as a round trip marked at the
    # final close (same convention as buy-and-hold); no hypothetical exit cost.
    if position == 1 and entry_equity:
        round_trip_pnls.append(equity[-1] / entry_equity - 1.0)

    return {
        "equity": equity,
        "in_market": in_market,
        "num_trades": trades,
        "entries": entries,
        "exits": exits,
        "round_trips": len(round_trip_pnls),
        "open_at_end": position == 1,
        "round_trip_pnls": round_trip_pnls,
    }


def run_buy_and_hold(
    closes: list[float], start_index: int, cost_rate: float, capital: float
) -> dict[str, object]:
    """Buy at the close of the first day of the same window, then never trade again."""
    units = (capital - capital * cost_rate) / closes[start_index]
    equity = [units * closes[j] for j in range(start_index, len(closes))]
    return {
        "equity": equity,
        "in_market": [True] * len(equity),
        "num_trades": 1,
        "entries": 1,
        "exits": 0,
        "round_trips": 1,
        "open_at_end": True,
        "round_trip_pnls": [equity[-1] / (capital - capital * cost_rate) - 1.0],
    }


def build_results(dataset_path: Path = DATASET_PATH) -> dict[str, object]:
    """Compute the full result document. Pure function of the committed dataset + CONFIG."""
    dates, closes = load_candles(dataset_path)

    fast_window = int(CONFIG["fast_window"])
    slow_window = int(CONFIG["slow_window"])
    warmup_rows = int(CONFIG["warmup_rows"])
    cost_rate = (float(CONFIG["fee_pct_per_side"]) + float(CONFIG["slippage_pct_per_side"])) / 100.0
    capital = float(CONFIG["start_capital_usd"])

    start_index = max(slow_window, warmup_rows) - 1
    window_dates = dates[start_index:]
    from datetime import date

    calendar_days = (date.fromisoformat(window_dates[-1]) - date.fromisoformat(window_dates[0])).days

    crossover = run_sma_crossover(dates, closes, start_index, cost_rate, capital)
    hold = run_buy_and_hold(closes, start_index, cost_rate, capital)

    metrics = {
        BASELINE_KEY: summarise(
            crossover["equity"], crossover["in_market"], capital, calendar_days,
            crossover["num_trades"], crossover["round_trip_pnls"],
        ),
        BUY_HOLD_KEY: summarise(
            hold["equity"], hold["in_market"], capital, calendar_days,
            hold["num_trades"], hold["round_trip_pnls"],
        ),
    }
    metrics[BASELINE_KEY]["round_trips"] = crossover["round_trips"]
    metrics[BUY_HOLD_KEY]["round_trips"] = hold["round_trips"]

    outperformance = metrics[BASELINE_KEY]["total_return_pct"] - metrics[BUY_HOLD_KEY]["total_return_pct"]

    results: dict[str, object] = {
        "script_version": SCRIPT_VERSION,
        "script": "scripts/run_baseline.py",
        "as_of_date": dates[-1],
        "dataset_path": str(dataset_path.relative_to(ROOT)) if dataset_path.is_relative_to(ROOT) else str(dataset_path),
        "dataset_rows": len(dates),
        "dataset_last_close": round2(closes[-1]),
        "period": {
            "start_date": window_dates[0],
            "end_date": window_dates[-1],
            "trading_days": len(window_dates),
            "calendar_days": calendar_days,
            "warmup_rows_used": start_index,
        },
        "params": {
            "strategy": CONFIG["strategy"],
            "fast_window": fast_window,
            "slow_window": slow_window,
            "warmup_rows": warmup_rows,
            "start_date_rule": "row max(slow_window, warmup_rows) of data/btc_market_daily.csv (1-based)",
            "fee_pct_per_side": round2(float(CONFIG["fee_pct_per_side"])),
            "slippage_pct_per_side": round2(float(CONFIG["slippage_pct_per_side"])),
            "cost_pct_per_fill_both_sides": round2(cost_rate * 100.0),
            "start_capital_usd": round2(capital),
            "signal_timing": "signal from closes up to and including day t; position applies from t+1, filled at the close of t+1",
            "cost_model": "fee + slippage charged on the executed notional of every fill (entry and exit)",
        },
        "randomness": "none (deterministic)",
        "seed": None,
        "rounding": "every *_pct and *_equity value is rounded to 2 decimals with Python's round()",
        "stale": False,
        "net_of_costs": True,
        "metrics": metrics,
        "outperformance_vs_buy_and_hold_pct": round2(outperformance),
        "notes": (
            "Price-only baseline on the committed BTC-USD daily candles; the node/mempool telemetry in "
            "data/btc_node_metrics.json has too few daily snapshots to be used as a backtest input. "
            "This is a measurement floor for future models, not trading advice and not a strategy recommendation."
        ),
    }

    # Diagnostics kept next to the metrics so a reader can see the fills behind them.
    results["diagnostics"] = {
        BASELINE_KEY: {
            "entries": crossover["entries"],
            "exits": crossover["exits"],
            "round_trips": crossover["round_trips"],
            "open_position_at_end": crossover["open_at_end"],
        },
        BUY_HOLD_KEY: {
            "entries": hold["entries"],
            "exits": hold["exits"],
            "round_trips": hold["round_trips"],
            "open_position_at_end": hold["open_at_end"],
        },
        "win_rate_basis": "share of round trips with positive net P&L after costs; a position open at the end counts as one round trip marked at the final close",
        "num_trades_basis": "fills executed (an entry or an exit each count; a completed round trip is two fills)",
    }
    return results


def check(results: dict[str, object], committed_path: Path = RESULTS_PATH) -> int:
    """Compare freshly computed metrics against the committed file. Fail only on real drift."""
    if not committed_path.is_file():
        print(f"baseline check: FAIL - {committed_path} is missing; run scripts/run_baseline.py")
        return 1
    committed = json.loads(committed_path.read_text(encoding="utf-8"))

    if committed.get("as_of_date") != results["as_of_date"]:
        print(
            "baseline check: WARN - committed results are stale "
            f"(results as_of_date={committed.get('as_of_date')}, dataset last candle={results['as_of_date']}). "
            "Regenerate with: python scripts/run_baseline.py"
        )
        return 0

    mismatches: list[str] = []
    for key in (BASELINE_KEY, BUY_HOLD_KEY):
        want = results["metrics"][key]  # type: ignore[index]
        have = (committed.get("metrics") or {}).get(key) or {}
        for metric in METRIC_KEYS + ["round_trips"]:
            if metric not in have:
                mismatches.append(f"{key}.{metric}: missing from committed results")
            elif abs(float(have[metric]) - float(want[metric])) > 0.01:
                mismatches.append(f"{key}.{metric}: committed {have[metric]} != recomputed {want[metric]}")
    if abs(
        float(committed.get("outperformance_vs_buy_and_hold_pct", 1e9))
        - float(results["outperformance_vs_buy_and_hold_pct"])
    ) > 0.01:
        mismatches.append(
            "outperformance_vs_buy_and_hold_pct: committed "
            f"{committed.get('outperformance_vs_buy_and_hold_pct')} != recomputed "
            f"{results['outperformance_vs_buy_and_hold_pct']}"
        )

    if mismatches:
        print(f"baseline check: FAIL - {len(mismatches)} drift(s) against {committed_path}")
        for item in mismatches:
            print(f"  - {item}")
        print("\nRe-run: python scripts/run_baseline.py")
        return 1

    print(
        f"baseline check: OK - committed results reproduce on {results['dataset_rows']} rows "
        f"(as_of_date {results['as_of_date']})"
    )
    return 0


def print_summary(results: dict[str, object]) -> None:
    metrics = results["metrics"]  # type: ignore[index]
    base, hold = metrics[BASELINE_KEY], metrics[BUY_HOLD_KEY]
    period = results["period"]  # type: ignore[index]
    params = results["params"]  # type: ignore[index]
    print("baseline: long-only SMA crossover vs buy-and-hold (net of costs)")
    print(
        f"  dataset   : {results['dataset_rows']} daily candles, last {results['as_of_date']}"
        f" (close {results['dataset_last_close']:,.2f})"
    )
    print(
        f"  window    : {period['start_date']} -> {period['end_date']}"
        f" ({period['trading_days']} trading days, {period['warmup_rows_used']} warm-up rows)"
    )
    print(
        f"  params    : SMA {params['fast_window']}/{params['slow_window']}, "
        f"fee {params['fee_pct_per_side']}%/side + slippage {params['slippage_pct_per_side']}%/side, "
        f"capital ${params['start_capital_usd']:,.2f}"
    )
    header = f"  {'metric':<36}{'SMA ' + str(params['fast_window']) + '/' + str(params['slow_window']):>14}{'buy & hold':>14}"
    print(header)
    for label, key in (
        ("total return %", "total_return_pct"),
        ("CAGR %", "cagr_pct"),
        ("max drawdown %", "max_drawdown_pct"),
        ("trades (fills)", "num_trades"),
        ("round trips", "round_trips"),
        ("win rate %", "win_rate_pct"),
        ("time in market %", "pct_time_in_market"),
        ("final equity $", "final_equity"),
    ):
        left = "n/a" if base[key] is None else f"{base[key]:,.2f}"
        right = "n/a" if hold[key] is None else f"{hold[key]:,.2f}"
        print(f"  {label:<36}{left:>14}{right:>14}")
    print(
        f"  outperformance vs buy-and-hold: {results['outperformance_vs_buy_and_hold_pct']:+.2f} "
        "percentage points (net of costs)"
    )
    print("  deterministic, no look-ahead: signal at close t, position from t+1")


def write_chart(results: dict[str, object], dataset_path: Path = DATASET_PATH) -> None:
    """Optional asset: equity curves. matplotlib is imported here only (see module docstring)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except ImportError:  # pragma: no cover - optional asset path
        print("chart: skipped (matplotlib not installed; see docs/assets/make_assets.py)")
        return

    from datetime import date

    dates, closes = load_candles(dataset_path)
    params = results["params"]  # type: ignore[index]
    start_index = max(int(params["slow_window"]), int(params["warmup_rows"])) - 1
    cost_rate = float(params["cost_pct_per_fill_both_sides"]) / 100.0
    capital = float(params["start_capital_usd"])
    crossover = run_sma_crossover(dates, closes, start_index, cost_rate, capital)
    hold = run_buy_and_hold(closes, start_index, cost_rate, capital)
    xs = [date.fromisoformat(d) for d in dates[start_index:]]

    fig, ax = plt.subplots(figsize=(10, 5), dpi=128)
    ax.plot(xs, crossover["equity"], linewidth=1.5, label=f"SMA {params['fast_window']}/{params['slow_window']} crossover", color="#0b7285")
    ax.plot(xs, hold["equity"], linewidth=1.5, label="buy & hold", color="#f08c00")
    ax.set_title(
        f"BTC-USD baseline equity, net of {params['cost_pct_per_fill_both_sides']}% per fill "
        f"({params['fee_pct_per_side']}% fee + {params['slippage_pct_per_side']}% slippage)"
    )
    ax.set_xlabel("close date (UTC)")
    ax.set_ylabel(f"equity, USD (start ${capital:,.0f})")
    ax.grid(alpha=0.3)
    ax.legend(loc="best", frameon=False)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    EQUITY_PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(EQUITY_PNG_PATH)
    plt.close(fig)
    print(f"chart: wrote {EQUITY_PNG_PATH.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SMA-crossover vs buy-and-hold baseline.")
    parser.add_argument("--check", action="store_true", help="compare a fresh run with results/baseline_results.json")
    parser.add_argument("--print-only", action="store_true", help="print the summary and write no files")
    parser.add_argument("--chart", action="store_true", help="also write results/baseline_equity.png (needs matplotlib)")
    parser.add_argument("--dataset", default=str(DATASET_PATH), help="dataset to read (default: data/btc_market_daily.csv)")
    parser.add_argument("--output", default=str(RESULTS_PATH), help="where to write the results JSON")
    args = parser.parse_args(argv)

    results = build_results(Path(args.dataset))

    if args.check:
        return check(results)

    print_summary(results)

    if args.print_only:
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    if args.chart:
        write_chart(results, Path(args.dataset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
