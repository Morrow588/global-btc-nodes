#!/usr/bin/env python3
"""Fetch live Bitcoin *network* telemetry and BTC market data into `data/`.

What this actually does (no JSON-RPC, no node of our own):

  Bitcoin network telemetry -- public, keyless HTTP APIs
    * mempool.space  /api/mempool              mempool tx count, vsize, total fee
                     /api/blocks/tip/height    chain tip height
                     /api/v1/fees/recommended  fee estimates (sat/vB)
    * bitnodes.io    /api/v1/snapshots/latest  reachable Bitcoin node count

  Market data -- public, keyless HTTP APIs (Kraken primary, Coinbase fallback)
    * Kraken         /0/public/OHLC?pair=XBTUSD&interval=1440  daily OHLCV
    * Coinbase       /products/BTC-USD/candles?granularity=86400

Outputs (both tracked in git, both written only after validation passes):

  data/btc_node_metrics.json  daily node/chain snapshot series (one entry per
                              UTC date, updated in place on a same-day re-run)
  data/btc_market_daily.csv   closed daily BTC-USD OHLCV candles, append-only

Guarantees:
  * Validate-then-write. Every value is range-checked, timestamps are checked
    to be strictly monotonic, and nulls are rejected. On any failure the script
    exits non-zero and *no tracked file is touched* -- a red run leaves the last
    good dataset exactly as it was. Files are never written empty or partial
    (writes go to a temp file and are moved into place atomically).
  * Idempotent. CSV rows are deduplicated by date (existing rows win, so the
    dataset is append-only), and the JSON upserts one entry per UTC date instead
    of appending. Re-running on the same day cannot duplicate anything.
  * --dry-run prints exactly what would be written and touches no files.

Only `requests` is required (plus the standard library).

Usage:
    python scripts/fetch_data.py                     # fetch and write
    python scripts/fetch_data.py --dry-run           # print, write nothing
    python scripts/fetch_data.py --summary-file /tmp/summary.txt
    python scripts/fetch_data.py --transport fixture --fixtures tests/fixtures

`--transport fixture` is the offline test hook: it reads each response from
`<fixtures>/<url-slug>.json` instead of the network, which is how the failure
paths and idempotency checks are exercised without depending on live APIs.

Exit codes: 0 success, 1 fetch/transport failure, 2 validation failure,
3 usage/configuration failure.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import requests

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

MEMPOOL_API = "https://mempool.space/api"
BITNODES_API = "https://bitnodes.io/api/v1"
KRAKEN_API = "https://api.kraken.com/0/public"
COINBASE_API = "https://api.exchange.coinbase.com"

MEMPOOL_URL = f"{MEMPOOL_API}/mempool"
TIP_HEIGHT_URL = f"{MEMPOOL_API}/blocks/tip/height"
FEES_URL = f"{MEMPOOL_API}/v1/fees/recommended"
BITNODES_URL = f"{BITNODES_API}/snapshots/latest/"
KRAKEN_URL = f"{KRAKEN_API}/OHLC?pair=XBTUSD&interval=1440"
COINBASE_URL = f"{COINBASE_API}/products/BTC-USD/candles?granularity=86400"

USER_AGENT = "virtualtradrs-dataset-fetcher/1.0 (+https://github.com/virtualtradrs)"

NODE_JSON = "btc_node_metrics.json"
MARKET_CSV = "btc_market_daily.csv"
CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "source"]

# Sane ranges. A value outside these is a corrupt response, not a market event.
RANGES: dict[str, tuple[float, float]] = {
    "tip_height": (700_000, 1_300_000),
    "mempool_tx_count": (0, 5_000_000),
    "mempool_vsize_bytes": (0, 20_000_000_000),
    "total_fee_sats": (0, 1_000_000_000_000),
    "fee_fastest_sat_vb": (1, 100_000),
    "fee_half_hour_sat_vb": (1, 100_000),
    "fee_hour_sat_vb": (1, 100_000),
    "fee_economy_sat_vb": (1, 100_000),
    "fee_minimum_sat_vb": (1, 100_000),
    "reachable_nodes": (100, 300_000),
    "total_nodes_crawled": (100, 300_000),
    "node_latest_height": (700_000, 1_300_000),
    "open": (0.01, 10_000_000),
    "high": (0.01, 10_000_000),
    "low": (0.01, 10_000_000),
    "close": (0.01, 10_000_000),
    "volume": (0, 1_000_000_000),
}

# bitnodes.io only re-crawls periodically; allow a day of lag against the chain tip.
TIP_LAG_TOLERANCE = 144


class FetchError(RuntimeError):
    """A source could not be read (network, HTTP status, or unparseable body)."""


class ValidationError(RuntimeError):
    """Fetched data is structurally or numerically impossible; nothing is written."""


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #


def url_slug(url: str) -> str:
    """Deterministic fixture filename for a URL: `https://a.io/x/y?z=1` -> `a.io_x_y_z_1.json`."""
    return re.sub(r"[^A-Za-z0-9]+", "_", url.replace("https://", "").replace("http://", "")).strip("_") + ".json"


@dataclass
class HttpTransport:
    """GET with real timeouts and bounded retries (429/5xx/connection errors)."""

    timeout: float = 20.0
    retries: int = 4
    backoff: float = 1.5
    sleep: Callable[[float], None] = time.sleep
    session: requests.Session = field(default_factory=requests.Session)

    def get_text(self, url: str) -> str:
        last_error = "unknown error"
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    url, timeout=self.timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                )
                if response.status_code == 200:
                    return response.text
                last_error = f"HTTP {response.status_code}"
                retryable = response.status_code == 429 or 500 <= response.status_code < 600
                if not retryable:
                    raise FetchError(f"GET {url} -> {last_error} (not retryable)")
                if response.status_code == 429:
                    # Honour Retry-After when the server sends it.
                    header = response.headers.get("Retry-After", "")
                    try:
                        delay = float(header)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                        except Exception:
                            delay = 0.0
                    if delay > 0:
                        self.sleep(min(delay, 60.0))
            except FetchError:
                raise
            except requests.RequestException as exc:  # timeouts, DNS, TLS, resets
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                self.sleep(self.backoff ** attempt)
        raise FetchError(f"GET {url} failed after {self.retries} attempts ({last_error})")

    def get_json(self, url: str) -> Any:
        text = self.get_text(url)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"GET {url} returned a non-JSON body: {exc}") from exc


@dataclass
class FixtureTransport:
    """Offline transport used by tests: one file per URL, no network."""

    directory: Path

    def get_text(self, url: str) -> str:
        path = self.directory / url_slug(url)
        if not path.is_file():
            raise FetchError(f"fixture missing for {url} (expected {path})")
        return path.read_text(encoding="utf-8")

    def get_json(self, url: str) -> Any:
        text = self.get_text(url)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"fixture {url_slug(url)} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Source-specific parsing
# --------------------------------------------------------------------------- #


def fetch_chain(transport: Any) -> dict[str, Any]:
    """mempool.space: mempool state, chain tip height and fee estimates."""
    mempool = transport.get_json(MEMPOOL_URL)
    if not isinstance(mempool, dict):
        raise FetchError(f"{MEMPOOL_URL}: expected a JSON object")

    raw_height = transport.get_text(TIP_HEIGHT_URL).strip()
    if not re.fullmatch(r"\d+", raw_height):
        raise FetchError(f"{TIP_HEIGHT_URL}: expected a bare integer, got {raw_height[:60]!r}")

    fees = transport.get_json(FEES_URL)
    if not isinstance(fees, dict):
        raise FetchError(f"{FEES_URL}: expected a JSON object")

    return {
        "tip_height": int(raw_height),
        "mempool_tx_count": mempool.get("count"),
        "mempool_vsize_bytes": mempool.get("vsize"),
        "total_fee_sats": mempool.get("total_fee"),
        "fee_fastest_sat_vb": fees.get("fastestFee"),
        "fee_half_hour_sat_vb": fees.get("halfHourFee"),
        "fee_hour_sat_vb": fees.get("hourFee"),
        "fee_economy_sat_vb": fees.get("economyFee"),
        "fee_minimum_sat_vb": fees.get("minimumFee"),
    }


def fetch_node_network(transport: Any) -> dict[str, Any] | None:
    """bitnodes.io reachable-node census. Best effort: a failure degrades, it does not block."""
    try:
        snapshot = transport.get_json(BITNODES_URL)
    except FetchError as exc:
        print(f"warning: bitnodes.io unreachable, continuing without node-network stats ({exc})", file=sys.stderr)
        return None
    if not isinstance(snapshot, dict):
        print("warning: bitnodes.io returned an unexpected payload, skipping node-network stats", file=sys.stderr)
        return None

    metadata = snapshot.get("metadata") or {}
    captured = snapshot.get("timestamp")
    stats: dict[str, Any] = {
        "source": "bitnodes.io/api/v1/snapshots/latest",
        "snapshot_timestamp": captured,
        "total_nodes_crawled": snapshot.get("total_nodes"),
        "node_latest_height": snapshot.get("latest_height"),
        # bitnodes reports the validated reachable count under metadata; fall back
        # to total_nodes for older snapshot shapes.
        "reachable_nodes": metadata.get("reachable_nodes", snapshot.get("total_nodes")),
        "reachability_percent": metadata.get("reachability_percent"),
        "round_complete": metadata.get("round_complete", metadata.get("complete")),
    }
    if isinstance(captured, (int, float)):
        stats["snapshot_age_seconds"] = max(0, int(_utcnow().timestamp() - captured))
    if any(stats.get(key) is None for key in ("reachable_nodes", "total_nodes_crawled", "node_latest_height")):
        print("warning: bitnodes.io payload missing census fields, skipping node-network stats", file=sys.stderr)
        return None
    return stats


def fetch_market_kraken(transport: Any) -> tuple[str, list[dict[str, Any]]]:
    payload = transport.get_json(KRAKEN_URL)
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = None
    if isinstance(result, dict):
        rows = next((value for key, value in result.items() if key != "last"), None)
    if not isinstance(rows, list) or not rows:
        raise FetchError(f"{KRAKEN_URL}: no OHLC rows in response")

    candles = []
    for row in rows:
        # [time, open, high, low, close, vwap, volume, count]
        if not isinstance(row, list) or len(row) < 7:
            raise FetchError(f"{KRAKEN_URL}: malformed OHLC row {row!r}")
        candles.append(
            {
                "date": datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": _to_float(row[1]),
                "high": _to_float(row[2]),
                "low": _to_float(row[3]),
                "close": _to_float(row[4]),
                "volume": _to_float(row[6]),
                "source": "kraken:XBTUSD",
            }
        )
    return "kraken:XBTUSD", candles


def fetch_market_coinbase(transport: Any) -> tuple[str, list[dict[str, Any]]]:
    rows = transport.get_json(COINBASE_URL)
    if not isinstance(rows, list) or not rows:
        raise FetchError(f"{COINBASE_URL}: no candles in response")

    candles = []
    for row in rows:
        # [time, low, high, open, close, volume], newest first
        if not isinstance(row, list) or len(row) < 6:
            raise FetchError(f"{COINBASE_URL}: malformed candle {row!r}")
        candles.append(
            {
                "date": datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": _to_float(row[3]),
                "high": _to_float(row[2]),
                "low": _to_float(row[1]),
                "close": _to_float(row[4]),
                "volume": _to_float(row[5]),
                "source": "coinbase:BTC-USD",
            }
        )
    return "coinbase:BTC-USD", candles


def fetch_market(transport: Any) -> tuple[str, list[dict[str, Any]]]:
    """Daily candles: Kraken first, Coinbase Exchange as the fallback."""
    errors = []
    for name, fetcher in (("kraken", fetch_market_kraken), ("coinbase", fetch_market_coinbase)):
        try:
            return fetcher(transport)
        except (FetchError, ValidationError, ValueError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
            print(f"warning: market source {name} failed, trying the next one ({exc})", file=sys.stderr)
    raise FetchError("all market data sources failed: " + "; ".join(errors))


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise FetchError(f"expected a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FetchError(f"expected a number, got {value!r}") from exc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _check_range(label: str, value: Any, bounds: tuple[float, float]) -> None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a number, got {value!r}")
    low, high = bounds
    if not low <= float(value) <= high:
        raise ValidationError(f"{label}: {value!r} outside the sane range [{low}, {high}]")


def validate_chain_snapshot(snapshot: dict[str, Any]) -> None:
    required = ["date", "captured_at", "chain"]
    for key in required:
        if key not in snapshot:
            raise ValidationError(f"node snapshot: missing required field '{key}'")
    _parse_date(snapshot["date"], "node snapshot date")
    _parse_iso(snapshot["captured_at"], "node snapshot captured_at")

    chain = snapshot["chain"]
    if not isinstance(chain, dict):
        raise ValidationError("node snapshot: 'chain' must be an object")
    for key in ("tip_height", "mempool_tx_count", "mempool_vsize_bytes", "total_fee_sats"):
        if key not in chain:
            raise ValidationError(f"node snapshot chain: missing required field '{key}'")
        _check_range(f"chain.{key}", chain[key], RANGES[key])
    for key in [k for k in RANGES if k.startswith("fee_")]:
        if key in chain and chain[key] is not None:
            _check_range(f"chain.{key}", chain[key], RANGES[key])

    network = snapshot.get("node_network")
    if network is not None:
        if not isinstance(network, dict):
            raise ValidationError("node snapshot: 'node_network' must be an object when present")
        for key in ("reachable_nodes", "total_nodes_crawled", "node_latest_height"):
            if key not in network:
                raise ValidationError(f"node_network: missing required field '{key}'")
            _check_range(f"node_network.{key}", network[key], RANGES[key])
        if abs(int(chain["tip_height"]) - int(network["node_latest_height"])) > TIP_LAG_TOLERANCE:
            raise ValidationError(
                "node_network.node_latest_height "
                f"{network['node_latest_height']} is more than {TIP_LAG_TOLERANCE} blocks behind the "
                f"chain tip {chain['tip_height']}: the two sources disagree"
            )


def validate_snapshot_series(snapshots: list[dict[str, Any]]) -> None:
    if not snapshots:
        raise ValidationError("node snapshot series is empty")
    seen: set[str] = set()
    previous: str | None = None
    for snapshot in snapshots:
        validate_chain_snapshot(snapshot)
        date = snapshot["date"]
        if date in seen:
            raise ValidationError(f"duplicate snapshot date {date}: one entry per UTC date is required")
        seen.add(date)
        if previous is not None and date <= previous:
            raise ValidationError(f"snapshot dates are not strictly increasing: {previous} then {date}")
        previous = date


def validate_candles(candles: list[dict[str, Any]]) -> None:
    if not candles:
        raise ValidationError("market dataset has no candles")
    previous: str | None = None
    for candle in candles:
        date = _parse_date(candle.get("date"), "candle date")
        if previous is not None and date <= previous:
            raise ValidationError(f"candle dates are not strictly increasing: {previous} then {date}")
        previous = date
        for key in ("open", "high", "low", "close", "volume"):
            if key not in candle:
                raise ValidationError(f"candle {date}: missing required field '{key}'")
            _check_range(f"candle {date} {key}", candle[key], RANGES[key])
        if not isinstance(candle.get("source"), str) or not candle["source"]:
            raise ValidationError(f"candle {date}: missing provenance field 'source'")
        high, low = float(candle["high"]), float(candle["low"])
        if high < low:
            raise ValidationError(f"candle {date}: high {high} is below low {low}")
        for name in ("open", "close"):
            if not low <= float(candle[name]) <= high:
                raise ValidationError(f"candle {date}: {name} {candle[name]} outside [low {low}, high {high}]")
        if float(candle["volume"]) < 0:
            raise ValidationError(f"candle {date}: negative volume {candle['volume']}")


def _parse_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label}: expected an ISO date string, got {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError(f"{label}: not a valid YYYY-MM-DD date: {value!r}") from exc
    if parsed.date() > _utcnow().date():
        raise ValidationError(f"{label}: {value} is in the future")
    return value


def _parse_iso(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label}: expected an ISO-8601 timestamp, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValidationError(f"{label}: not a valid YYYY-MM-DDTHH:MM:SSZ timestamp: {value!r}") from exc
    return value


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #


def build_snapshot(chain: dict[str, Any], node_network: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "date": now.strftime("%Y-%m-%d"),
        "captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain": dict(chain),
    }
    vsize = chain.get("mempool_vsize_bytes")
    if isinstance(vsize, (int, float)):
        snapshot["chain"]["mempool_size_mb"] = round(float(vsize) / 1_000_000, 3)
    if node_network is not None:
        snapshot["node_network"] = node_network
    return snapshot


def load_json_dataset(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "snapshots": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"existing {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("snapshots"), list):
        raise ValidationError(f"existing {path.name} does not have a 'snapshots' list")
    # Anything already committed must itself be valid, otherwise we would be
    # blessing a corrupt baseline with new data.
    validate_snapshot_series(document["snapshots"])
    return document


def _observable(entry: dict[str, Any]) -> dict[str, Any]:
    """Snapshot fields that are *data* (everything except capture bookkeeping)."""
    return {key: value for key, value in entry.items() if key not in ("captured_at", "observation_count", "first_seen_at")}


def upsert_snapshot(document: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """Replace the entry for this UTC date, or insert it.

    Returns 'added', 'updated' or 'unchanged'. When a re-run observes exactly the
    same values as the stored snapshot (the frozen-fixture case), the stored entry
    is kept verbatim -- including its original `captured_at` -- so a same-day
    re-run rewrites nothing and produces a byte-identical file.
    """
    snapshots = document["snapshots"]
    for index, existing in enumerate(snapshots):
        if existing.get("date") == snapshot["date"]:
            if _observable(existing) == _observable(snapshot):
                return "unchanged"
            merged = dict(snapshot)
            merged["observation_count"] = int(existing.get("observation_count", 1)) + 1
            merged["first_seen_at"] = existing.get("first_seen_at", existing.get("captured_at"))
            snapshots[index] = merged
            return "updated"
    snapshot["observation_count"] = 1
    snapshot["first_seen_at"] = snapshot["captured_at"]
    snapshots.append(snapshot)
    return "added"


def trim_snapshots(document: dict[str, Any], max_days: int) -> None:
    snapshots = sorted(document["snapshots"], key=lambda item: item["date"])
    document["snapshots"] = snapshots[-max_days:]


def load_candles(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_COLUMNS:
            raise ValidationError(
                f"existing {path.name} header is {reader.fieldnames}, expected {CSV_COLUMNS}"
            )
        for record in reader:
            row: dict[str, Any] = {"date": record["date"], "source": record["source"]}
            for key in ("open", "high", "low", "close", "volume"):
                try:
                    row[key] = float(record[key])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"existing {path.name}: {record['date']} has a non-numeric {key}") from exc
            rows.append(row)
    validate_candles(rows)
    return rows


def merge_candles(existing: list[dict[str, Any]], fetched: list[dict[str, Any]], today: str) -> list[int]:
    """Append closed candles only. Existing rows win, so the CSV is append-only.

    The in-progress UTC day is dropped: an unfinished candle is not a daily bar,
    and dropping it is what makes a same-day re-run produce an identical file.
    Returns the indices of the newly appended rows.
    """
    by_date = {row["date"]: row for row in existing}
    added: list[int] = []
    for candle in sorted(fetched, key=lambda item: item["date"]):
        if candle["date"] >= today:
            continue  # still open
        if candle["date"] in by_date:
            continue  # already recorded; never rewritten
        existing.append(candle)
        by_date[candle["date"]] = candle
        added.append(len(existing) - 1)
    existing.sort(key=lambda row: row["date"])
    return added


def render_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "date": row["date"],
                "open": f"{row['open']:.2f}",
                "high": f"{row['high']:.2f}",
                "low": f"{row['low']:.2f}",
                "close": f"{row['close']:.2f}",
                "volume": f"{row['volume']:.8f}".rstrip("0").rstrip("."),
                "source": row["source"],
            }
        )
    return buffer.getvalue()


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    """Atomic write; returns True if the file's bytes changed (or it is new)."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    if existing == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(content)
        os.chmod(handle.name, 0o644)  # NamedTemporaryFile creates 0600; keep repo files world-readable
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return True


# --------------------------------------------------------------------------- #
# Summary / commit message
# --------------------------------------------------------------------------- #


def build_summary(
    snapshot: dict[str, Any],
    added: int,
    total_candles: int,
    status: str,
    market_source: str,
    changed: bool,
) -> tuple[str, str]:
    chain = snapshot["chain"]
    network = snapshot.get("node_network") or {}
    size_mb = chain.get("mempool_size_mb")
    size_text = f"{size_mb:.1f} MB" if isinstance(size_mb, (int, float)) else "size n/a"
    if added == 0:
        candle_text = "no new candles"
    else:
        candle_text = f"+{added} candle" + ("" if added == 1 else "s")

    subject = (
        f"data: btc node + market snapshot {snapshot['date']} "
        f"(height {chain['tip_height']}, mempool {size_text}, {candle_text})"
    )
    node_line = (
        f"- node network: {network.get('reachable_nodes')} reachable nodes "
        f"({network.get('total_nodes_crawled')} crawled, bitnodes.io snapshot "
        f"{datetime.fromtimestamp(network['snapshot_timestamp'], tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})"
        if network
        else "- node network: bitnodes.io unreachable on this run (chain + market data still recorded)"
    )
    lines = [
        subject,
        "",
        f"- chain: tip height {chain['tip_height']}, mempool {chain['mempool_tx_count']} txs / "
        f"{size_text}, median fee estimate {chain.get('fee_half_hour_sat_vb')} sat/vB",
        node_line,
        f"- market: {candle_text} from {market_source}; {total_candles} closed daily candles in "
        f"data/{MARKET_CSV}",
        f"- data/{NODE_JSON}: snapshot for {snapshot['date']} {status} "
        f"(observation {snapshot['observation_count']})",
        "",
        f"Sources: mempool.space, bitnodes.io, {market_source}.",
        f"Dataset files changed: {'yes' if changed else 'no (already up to date)'}",
    ]
    return subject, "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch live Bitcoin network telemetry and BTC market data into data/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[-1],
    )
    parser.add_argument("--data-dir", default=None, help="output directory (default: <repo>/data)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written, touch no files")
    parser.add_argument("--summary-file", default=None, help="also write the commit summary to this path")
    parser.add_argument("--transport", choices=["http", "fixture"], default="http")
    parser.add_argument("--fixtures", default=None, help="fixture directory for --transport fixture")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="attempts per URL (default: 4)")
    parser.add_argument("--max-days", type=int, default=730, help="max snapshots/candles kept per file")
    return parser.parse_args(argv)


def build_transport(args: argparse.Namespace) -> Any:
    if args.transport == "fixture":
        if not args.fixtures:
            raise SystemExit("--transport fixture requires --fixtures DIR")
        directory = Path(args.fixtures)
        if not directory.is_dir():
            raise SystemExit(f"fixture directory not found: {directory}")
        return FixtureTransport(directory)
    return HttpTransport(timeout=args.timeout, retries=args.retries)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    data_dir = Path(args.data_dir) if args.data_dir else repo_root / "data"
    json_path = data_dir / NODE_JSON
    csv_path = data_dir / MARKET_CSV
    started = time.time()

    try:
        transport = build_transport(args)
    except SystemExit as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    try:
        print(f"fetching chain + mempool telemetry from {MEMPOOL_API} ...")
        chain = fetch_chain(transport)
        print(f"fetching market candles ...")
        market_source, fetched_candles = fetch_market(transport)
        node_network = fetch_node_network(transport)

        now = _utcnow()
        snapshot = build_snapshot(chain, node_network, now)

        validate_chain_snapshot(snapshot)
        # Reading the current datasets is what makes --dry-run show the *diff* it
        # would produce (nothing is written, so the guarantee still holds).
        document = load_json_dataset(json_path)
        snapshots_before = len(document["snapshots"])
        existing_dates = {entry["date"] for entry in document["snapshots"]}
        node_status = upsert_snapshot(document, snapshot)
        trim_snapshots(document, args.max_days)
        validate_snapshot_series(document["snapshots"])
        written_snapshot = next(entry for entry in document["snapshots"] if entry["date"] == snapshot["date"])
        replaced = snapshot["date"] in existing_dates

        existing_candles = load_candles(csv_path)
        rows_before = len(existing_candles)
        today = now.strftime("%Y-%m-%d")
        added_indices = merge_candles(existing_candles, fetched_candles, today)
        validate_candles(existing_candles)
        trim_candles(existing_candles, args.max_days)

        json_content = render_json(document)
        csv_content = render_csv(existing_candles)
    except (FetchError, ValidationError) as exc:
        kind = "validation" if isinstance(exc, ValidationError) else "fetch"
        print(f"error: {kind} failure: {exc}", file=sys.stderr)
        print(f"error: no tracked file was modified; the last good dataset in {data_dir} is intact", file=sys.stderr)
        return 2 if isinstance(exc, ValidationError) else 1

    added = len(added_indices)
    new_dates = [existing_candles[index]["date"] for index in added_indices]
    subject, summary = build_summary(
        written_snapshot, added, len(existing_candles), node_status, market_source, changed=True,
    )

    print()
    print(f"chain tip height        : {chain['tip_height']}")
    print(f"mempool                 : {chain['mempool_tx_count']} txs / {snapshot['chain'].get('mempool_size_mb')} MB")
    print(f"fee estimate (30 min)   : {chain.get('fee_half_hour_sat_vb')} sat/vB")
    if node_network:
        print(f"reachable nodes         : {node_network['reachable_nodes']} (bitnodes.io)")
    print(f"market source           : {market_source}")
    print(f"new closed candles      : {added} ({new_dates[0] + ' .. ' + new_dates[-1] if new_dates else 'none'})")

    if args.dry_run:
        print()
        print(f"--- dry run: {json_path} ---")
        print(
            f"schema_version {document.get('schema_version')} | snapshots {snapshots_before} -> "
            f"{len(document['snapshots'])} | entry for {snapshot['date']} "
            f"{'replaced in place' if replaced else 'appended'}"
        )
        print(json.dumps(written_snapshot, indent=2))
        print(f"--- dry run: {csv_path} ---")
        print(
            f"columns {','.join(CSV_COLUMNS)} | rows {rows_before} -> {len(existing_candles)} | "
            f"{added} new row(s) appended, existing rows untouched"
        )
        for index in added_indices:
            row = existing_candles[index]
            print(
                f"{row['date']},{row['open']:.2f},{row['high']:.2f},{row['low']:.2f},"
                f"{row['close']:.2f},{row['volume']:.8f},{row['source']}"
            )
        print("--- dry run: nothing written; tracked files untouched ---")
    else:
        json_changed = write_if_changed(json_path, json_content)
        csv_changed = write_if_changed(csv_path, csv_content)
        subject, summary = build_summary(
            written_snapshot, added, len(existing_candles), node_status, market_source,
            changed=json_changed or csv_changed,
        )
        print()
        print(f"{'updated' if json_changed else 'unchanged'}: {json_path}")
        print(f"{'updated' if csv_changed else 'unchanged'}: {csv_path} ({len(existing_candles)} rows)")

    if args.summary_file:
        if args.dry_run:
            print(f"dry run: would write the commit summary to {args.summary_file}")
        else:
            Path(args.summary_file).write_text(summary, encoding="utf-8")
            print(f"summary written: {args.summary_file}")
    print()
    print(f"summary: {subject}")
    print(f"done in {time.time() - started:.1f}s")
    return 0


def trim_candles(rows: list[dict[str, Any]], max_days: int) -> None:
    if len(rows) > max_days:
        del rows[: len(rows) - max_days]


if __name__ == "__main__":
    sys.exit(main())
