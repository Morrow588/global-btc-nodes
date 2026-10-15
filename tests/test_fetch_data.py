"""Offline regression tests; every dataset and HTTP fixture lives in a temp directory."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import fetch_data as fetch


NOW = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)


def snapshot(date="2026-01-02"):
    return {
        "date": date,
        "captured_at": f"{date}T12:00:00Z",
        "chain": {
            "tip_height": 900_000,
            "mempool_tx_count": 100,
            "mempool_vsize_bytes": 1_000_000,
            "total_fee_sats": 50_000,
            "fee_fastest_sat_vb": 5,
        },
    }


def candle(date="2026-01-02"):
    return {
        "date": date,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 1.0,
        "source": "kraken",
    }


class ValidationTests(unittest.TestCase):
    def test_valid_inputs(self):
        fetch.validate_chain_snapshot(snapshot())
        fetch.validate_snapshot_series([snapshot(), snapshot("2026-01-03")])
        fetch.validate_candles([candle()])

    def test_missing_or_null_required_chain_values(self):
        for key in ("tip_height", "mempool_tx_count", "mempool_vsize_bytes", "total_fee_sats"):
            for missing in (False, True):
                with self.subTest(key=key, missing=missing):
                    value = snapshot()
                    if missing:
                        del value["chain"][key]
                    else:
                        value["chain"][key] = None
                    with self.assertRaises(fetch.ValidationError):
                        fetch.validate_chain_snapshot(value)

    def test_snapshot_dates_must_be_unique_and_increasing(self):
        for dates in (("2026-01-02", "2026-01-02"), ("2026-01-03", "2026-01-02")):
            with self.subTest(dates=dates), self.assertRaises(fetch.ValidationError):
                fetch.validate_snapshot_series([snapshot(date) for date in dates])

    def test_ranges_reject_invalid_values(self):
        for key, value in (("fee_fastest_sat_vb", -1), ("tip_height", 1_300_001)):
            with self.subTest(key=key), self.assertRaises(fetch.ValidationError):
                fetch._check_range(key, value, fetch.RANGES[key])

    def test_range_endpoints_are_valid(self):
        for key, bounds in fetch.RANGES.items():
            for value in bounds:
                with self.subTest(key=key, value=value):
                    fetch._check_range(key, value, bounds)

    def test_invalid_candles(self):
        for changes in (
            {"close": None},
            {"volume": -1},
            {"high": 80},
            {"close": 120},
            {"source": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(fetch.ValidationError):
                fetch.validate_candles([{**candle(), **changes}])

    def test_candle_dates_must_be_unique_and_increasing(self):
        for dates in (("2026-01-02", "2026-01-02"), ("2026-01-03", "2026-01-02")):
            with self.subTest(dates=dates), self.assertRaises(fetch.ValidationError):
                fetch.validate_candles([candle(date) for date in dates])


class AtomicWriteTests(unittest.TestCase):
    def test_identical_content_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text("original\n", encoding="utf-8")
            before = path.stat()
            with patch.object(fetch.os, "replace") as replace:
                self.assertFalse(fetch.write_if_changed(path, "original\n"))
            replace.assert_not_called()
            self.assertEqual(path.read_bytes(), b"original\n")
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_changed_content_is_replaced_from_same_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text("original\n", encoding="utf-8")
            real_replace = os.replace

            def inspect_replace(source, destination):
                self.assertEqual(Path(source).parent, path.parent)
                self.assertEqual(Path(source).read_bytes(), b"new\n")
                self.assertEqual(path.read_bytes(), b"original\n")
                real_replace(source, destination)

            with patch.object(fetch.os, "replace", side_effect=inspect_replace) as replace:
                self.assertTrue(fetch.write_if_changed(path, "new\n"))
            replace.assert_called_once()
            self.assertEqual(path.read_bytes(), b"new\n")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_new_file_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "dataset.json"
            self.assertTrue(fetch.write_if_changed(path, "new\n"))
            self.assertEqual(path.read_bytes(), b"new\n")

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text("original\n", encoding="utf-8")
            with patch.object(fetch.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    fetch.write_if_changed(path, "new\n")
            self.assertEqual(path.read_bytes(), b"original\n")
            self.assertEqual(list(path.parent.iterdir()), [path])


class PipelineTests(unittest.TestCase):
    def test_validation_failure_preserves_both_dataset_files(self):
        # Bad chain data fails early; bad candles fail after the in-memory
        # snapshot update, catching a premature JSON write as well.
        for bad_chain in (True, False):
            with self.subTest(bad_chain=bad_chain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                data = root / "data"
                fixtures = root / "fixtures"
                data.mkdir()
                fixtures.mkdir()
                node_path = data / fetch.NODE_JSON
                csv_path = data / fetch.MARKET_CSV
                node_path.write_text(
                    fetch.render_json({"schema_version": 1, "snapshots": [snapshot()]})
                )
                csv_path.write_text(fetch.render_csv([candle("2026-01-01")]))
                originals = {path: path.read_bytes() for path in (node_path, csv_path)}

                payloads = {
                    fetch.MEMPOOL_URL: {
                        "count": None if bad_chain else 100,
                        "vsize": 1_000_000,
                        "total_fee": 50_000,
                    },
                    fetch.TIP_HEIGHT_URL: 900_000,
                    fetch.FEES_URL: {"fastestFee": 5},
                    fetch.BITNODES_URL: {},
                    fetch.KRAKEN_URL: {
                        "result": {
                            "XXBTZUSD": [
                                [
                                    int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()),
                                    "100",
                                    "110",
                                    "90",
                                    "105",
                                    "100",
                                    "1" if bad_chain else "-1",
                                    1,
                                ]
                            ],
                            "last": 0,
                        }
                    },
                }
                for url, payload in payloads.items():
                    (fixtures / fetch.url_slug(url)).write_text(json.dumps(payload))

                stderr = StringIO()
                with (
                    patch.object(fetch, "_utcnow", return_value=NOW),
                    patch.object(
                        fetch.requests.Session,
                        "request",
                        side_effect=AssertionError("network forbidden"),
                    ),
                    redirect_stdout(StringIO()),
                    redirect_stderr(stderr),
                ):
                    result = fetch.main(
                        [
                            "--data-dir",
                            str(data),
                            "--transport",
                            "fixture",
                            "--fixtures",
                            str(fixtures),
                        ]
                    )
                self.assertEqual(result, 2, stderr.getvalue())
                self.assertIn("validation failure", stderr.getvalue())
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)
                self.assertEqual(set(data.iterdir()), set(originals))


if __name__ == "__main__":
    unittest.main()
