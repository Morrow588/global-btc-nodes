# results/

Generated experiment artifacts - do not hand-edit; they are rewritten by the script that produced them.

- `baseline_results.json` - SMA 20/100 crossover vs buy-and-hold on `data/btc_market_daily.csv`, net of costs; written by `scripts/run_baseline.py` (schema and assumptions in that script's docstring, `--check` re-verifies it against the committed dataset).
- `baseline_equity.png` - the same two equity curves, drawn from the same run (`--chart`; matplotlib is an asset-only dependency like `docs/assets/`, not part of `requirements.txt`).

Regenerate from a clean clone: `python scripts/run_baseline.py` (JSON) or `python scripts/run_baseline.py --chart` (JSON + PNG, needs `pip install matplotlib`).
