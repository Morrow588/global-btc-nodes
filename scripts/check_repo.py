#!/usr/bin/env python3
"""Repository health check.

Verifies that the files and wiring this repository depends on are present and
still coherent: community files, the CI workflow, Dependabot configuration,
issue templates, and the README's structure (required sections, balanced code
fences, a Mermaid block for the architecture diagram).

Runs on stdlib only so it works in a bare checkout and in CI before any
dependency install. Exits non-zero and prints a readable list of problems.

Usage:
    python scripts/check_repo.py [--root PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "requirements.txt",
    ".gitignore",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/good_first_issue.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    "data/README.md",
    "data/btc_node_metrics.json",
    "data/btc_market_daily.csv",
    "scripts/fetch_data.py",
    ".github/workflows/auto-update.yml",
]

README_SECTIONS = [
    "## Why this exists",
    "## Architecture",
    "## How it works",
    "## Showcase",
    "## Quickstart",
    "## Reproducible Quantitative Research",
    "## Contributing",
    "## Roadmap",
    "## License",
]


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def check_files(root: Path, problems: list[str]) -> None:
    for rel in REQUIRED_FILES:
        path = root / rel
        if not path.is_file():
            problems.append(f"missing file: {rel}")
        elif path.stat().st_size == 0:
            problems.append(f"empty file: {rel}")


def check_readme(root: Path, problems: list[str]) -> None:
    path = root / "README.md"
    if not path.is_file():
        return  # already reported by check_files
    text = path.read_text(encoding="utf-8")

    title = first_heading(text)
    if not title or "AI Algorithmic Trading" not in title:
        problems.append("README.md: H1 must contain 'AI Algorithmic Trading'")

    for section in README_SECTIONS:
        if section not in text:
            problems.append(f"README.md: missing section '{section}'")

    # Unbalanced fences break every code block after them on GitHub.
    fences = len(re.findall(r"^```", text, flags=re.MULTILINE))
    if fences % 2 != 0:
        problems.append(f"README.md: unbalanced code fences ({fences} fence markers)")

    if not re.search(r"^```mermaid$", text, flags=re.MULTILINE):
        problems.append("README.md: no ```mermaid architecture diagram found")

    badges = len(re.findall(r"img\.shields\.io", text))
    if badges < 4:
        problems.append(f"README.md: expected at least 4 shields.io badges, found {badges}")


def check_workflow(root: Path, problems: list[str]) -> None:
    path = root / ".github/workflows/ci.yml"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if "check_repo.py" not in text:
        problems.append(".github/workflows/ci.yml: does not run scripts/check_repo.py")
    if not re.search(r"^\s*pull_request:", text, flags=re.MULTILINE):
        problems.append(".github/workflows/ci.yml: no pull_request trigger")


def check_dependabot(root: Path, problems: list[str]) -> None:
    path = root / ".github/dependabot.yml"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for ecosystem in ("pip", "github-actions"):
        if f"package-ecosystem: {ecosystem}" not in text:
            problems.append(f".github/dependabot.yml: missing '{ecosystem}' ecosystem")


def check_issue_templates(root: Path, problems: list[str]) -> None:
    config = root / ".github/ISSUE_TEMPLATE/config.yml"
    if config.is_file() and not re.search(
        r"^blank_issues_enabled:\s*false", config.read_text(encoding="utf-8"), flags=re.MULTILINE
    ):
        problems.append(".github/ISSUE_TEMPLATE/config.yml: blank_issues_enabled must be false")

    for name in ("bug_report.yml", "feature_request.yml", "good_first_issue.yml"):
        path = root / ".github/ISSUE_TEMPLATE" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for field in ("name:", "description:", "body:"):
            if not re.search(rf"^{field}", text, flags=re.MULTILINE):
                problems.append(f".github/ISSUE_TEMPLATE/{name}: missing '{field.strip(':')}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    problems: list[str] = []

    check_files(root, problems)
    check_readme(root, problems)
    check_workflow(root, problems)
    check_dependabot(root, problems)
    check_issue_templates(root, problems)

    checked = len(REQUIRED_FILES) + len(README_SECTIONS)
    checked += check_auto_update_workflow(root, problems)
    checked += check_dataset(root, problems)
    if problems:
        print(f"check_repo: {len(problems)} problem(s) found in {root}\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFix the items above and run again.")
        return 1

    print(f"check_repo: OK - {checked} checks passed in {root}")
    return 0


DATASET_CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "source"]


def check_auto_update_workflow(root: Path, problems: list[str]) -> int:
    """The scheduled job must be able to commit real data, and only when it moved."""
    path = root / ".github/workflows/auto-update.yml"
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    checks = [
        (r"^\s*schedule:", "no schedule trigger"),
        (r"^\s*workflow_dispatch:", "no workflow_dispatch trigger"),
        (r"^\s*contents: write", "permissions: contents: write missing"),
        (r"^\s*concurrency:", "no concurrency guard"),
        (r"cron:", "no cron expression"),
        ("fetch_data.py", "does not run scripts/fetch_data.py"),
        ("git status --porcelain", "commit is not guarded on an actual data change"),
        ("commit -F", "does not commit with the generated message"),
        ("check_repo.py", "does not re-check the repository after fetching"),
    ]
    for pattern, message in checks:
        if not re.search(pattern, text, flags=re.MULTILINE):
            problems.append(f".github/workflows/auto-update.yml: {message}")
    return len(checks)


def check_dataset(root: Path, problems: list[str]) -> int:
    """The committed dataset must match the schema the pipeline documents."""
    import csv
    import json

    checks = 0
    json_path = root / "data/btc_node_metrics.json"
    if json_path.is_file():
        try:
            document = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"data/btc_node_metrics.json: not valid JSON ({exc})")
            document = None
        if isinstance(document, dict):
            snapshots = document.get("snapshots")
            checks += 1
            if not isinstance(snapshots, list) or not snapshots:
                problems.append("data/btc_node_metrics.json: 'snapshots' must be a non-empty list")
            else:
                dates = [entry.get("date") for entry in snapshots]
                checks += 1
                if dates != sorted(dates) or len(set(dates)) != len(dates):
                    problems.append("data/btc_node_metrics.json: snapshot dates must be unique and increasing")
                for entry in snapshots:
                    checks += 1
                    chain = entry.get("chain") or {}
                    missing = [k for k in ("tip_height", "mempool_tx_count", "mempool_vsize_bytes") if chain.get(k) is None]
                    if missing:
                        problems.append(f"data/btc_node_metrics.json: {entry.get('date')} missing chain fields {missing}")
    csv_path = root / "data/btc_market_daily.csv"
    if csv_path.is_file():
        import csv as _csv

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = _csv.DictReader(handle)
            checks += 1
            if reader.fieldnames != DATASET_CSV_COLUMNS:
                problems.append(f"data/btc_market_daily.csv: header is {reader.fieldnames}, expected {DATASET_CSV_COLUMNS}")
            rows = list(reader)
        checks += 1
        if not rows:
            problems.append("data/btc_market_daily.csv: no data rows")
        else:
            dates = [row.get("date") for row in rows]
            checks += 1
            if dates != sorted(dates) or len(set(dates)) != len(dates):
                problems.append("data/btc_market_daily.csv: rows must be sorted by unique date")
    return checks


if __name__ == "__main__":
    sys.exit(main())
