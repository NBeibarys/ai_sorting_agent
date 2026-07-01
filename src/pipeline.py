"""
Batch driver: reads application rows from a Google Sheet, classifies each
startup's HQ country via the ADK agent (or a local heuristic in --dry-run),
groups rows into country buckets, and writes one tab per target country back
INTO THE SAME SHEET (columns: Startup Name, Timestamp, incorporated, HQ country).

Rows are processed concurrently because they are fully independent — no shared
state between applicants — so a ThreadPoolExecutor is sufficient; no need for
asyncio's added complexity for what is mostly I/O-bound work (API calls).
"""
import csv
import json
import os
import re
import unicodedata
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .google_clients import (
    color_name_cell,
    create_sheet_tab,
    get_sheet_id_by_title,
    get_sheets_service,
    read_sheet_rows,
    write_tab_data,
)

# Ordered target buckets -> sheet tab titles (<=31 chars each; Sheets/Excel
# cap tab names at 31). The last combines Mongolia, Turkmenistan, Tajikistan.
TARGET_TABS = OrderedDict([
    ("Uzbekistan", "Uzbekistan"),
    ("Turkiye", "Turkiye"),
    ("Georgia", "Georgia"),
    ("Kyrgyzstan", "Kyrgyzstan"),
    ("Azerbaijan", "Azerbaijan"),
    ("USA", "USA"),
    ("Kazakhstan", "Kazakhstan"),
    ("Mong. Turkmenistan Tajikistan", "Mong. Turkmenistan Tajikistan"),
])

# Substring keys used to locate the messy form columns robustly (headers
# sometimes carry trailing spaces or slight rewordings across cohorts).
COL_NEEDLES = {
    "timestamp": ["timestamp"],
    "incorporated": ["incorporated"],
}

# Needles that are nice-to-have but not required: a missing column here
# resolves to index None instead of raising (the row loop emits "" for it).
OPTIONAL_NEEDLES = {"incorporated"}


def _norm(text: str) -> str:
    """Accent-folded lowercase for heuristic matching only."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(c)
    ).lower().strip()


def _find_columns(header: list, config: Config) -> dict:
    """Locate columns by case-insensitive substring; return 0-indexed positions.

    The country and startup-name needles come from Config (SORTER_COUNTRY_COLUMN
    / SORTER_NAME_COLUMN) so they can be re-pointed per deployment without
    editing code; timestamp stays as a fixed default in COL_NEEDLES. Matching is
    case-insensitive and accent-folded (see _norm).

    Fails loudly if any expected column is missing — a silent None index would
    misclassify every row, so we abort before touching the API.
    """
    needles = {
        **COL_NEEDLES,
        "name": [config.name_column],
        "country": [config.country_column],
    }
    lowered = [(_norm(h), i) for i, h in enumerate(header)]
    found = {}
    for key, key_needles in needles.items():
        # Normalize needles too (accent-fold + lowercase) so matching is
        # truly case-insensitive, matching the docstring. Without this a
        # mixed-case SORTER_*_COLUMN value never matches a lowercased
        # header and every column lookup falsely fails.
        norm_needles = [_norm(n) for n in key_needles]
        idx = next(
            (i for low, i in lowered if any(n in low for n in norm_needles)),
            None,
        )
        if idx is None and key not in OPTIONAL_NEEDLES:
            raise RuntimeError(
                f"Could not find the '{key}' column. Expected a header containing "
                f"one of {key_needles}. Got headers: {header[:6]}..."
            )
        found[key] = idx  # may be None for optional needles (e.g. incorporated)
    return found


# --- deterministic fallback (used by --dry-run; no API calls) ---
_CITY_COUNTRY = {
    "tashkent": "Uzbekistan", "toshkent": "Uzbekistan", "samarkand": "Uzbekistan",
    "andijan": "Uzbekistan", "andijon": "Uzbekistan", "namangan": "Uzbekistan",
    "fergana": "Uzbekistan", "fargona": "Uzbekistan", "nukus": "Uzbekistan",
    "karakalpakstan": "Uzbekistan", "qoraqolpog": "Uzbekistan",
    "astana": "Kazakhstan", "almaty": "Kazakhstan", "karaganda": "Kazakhstan",
    "uralsk": "Kazakhstan", "petropavlovsk": "Kazakhstan",
    "bishkek": "Kyrgyzstan",
    "tbilisi": "Georgia",
    "baku": "Azerbaijan",
    "istanbul": "Turkiye", "ankara": "Turkiye", "izmir": "Turkiye",
    "ulaanbaatar": "Mong. Turkmenistan Tajikistan",
    "dushanbe": "Mong. Turkmenistan Tajikistan",
    "ashgabat": "Mong. Turkmenistan Tajikistan",
}
_COUNTRY_SYNONYMS = {
    "uzbekistan": "Uzbekistan", "uzbekiston": "Uzbekistan", "ozbekiston": "Uzbekistan",
    "kazakhstan": "Kazakhstan", "kazahstan": "Kazakhstan", "kazakshtan": "Kazakhstan",
    "kyrgyzstan": "Kyrgyzstan", "kyrgyz republic": "Kyrgyzstan",
    "georgia": "Georgia",
    "azerbaijan": "Azerbaijan",
    "turkiye": "Turkiye", "turkey": "Turkiye",
    "united states": "USA", "united states of america": "USA", "san francisco": "USA",
    "mongolia": "Mong. Turkmenistan Tajikistan",
    "turkmenistan": "Mong. Turkmenistan Tajikistan",
    "tajikistan": "Mong. Turkmenistan Tajikistan",
}


def deterministic_classify(country_raw: str) -> str:
    """Local heuristic classifier — only for --dry-run verification."""
    text = _norm(country_raw)
    if not text:
        return "Other"
    for city, bucket in _CITY_COUNTRY.items():
        if city in text:
            return bucket
    for syn, bucket in _COUNTRY_SYNONYMS.items():
        if re.search(rf"\b{re.escape(syn)}\b", text):
            return bucket
    # bare country codes
    tokens = set(text.replace(",", " ").split())
    if tokens.intersection({"usa", "us"}):
        return "USA"
    if "kz" in tokens:
        return "Kazakhstan"
    return "Other"


def _load_checkpoint(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_checkpoint(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"WARNING: could not save checkpoint: {exc}", flush=True)


def _print_summary(grouped: dict, other_log: list, total: int) -> None:
    print("\n=== Summary ===", flush=True)
    placed = 0
    for bucket in TARGET_TABS:
        n = len(grouped.get(bucket, []))
        placed += n
        print(f"  {bucket:32s} {n}")
    print(f"  {'(excluded / Other)':32s} {len(other_log)}")
    print(f"  {'TOTAL rows':32s} {total}")
    if other_log:
        # Show where the excluded ones went, so misclassifications are visible.
        from collections import Counter
        # other_log rows are (name, ts, incorporated_raw, country_raw, bucket);
        # the classified bucket is the last element.
        buckets = Counter(t[-1] for t in other_log)
        print("\nExcluded breakdown (top):")
        for b, n in buckets.most_common(15):
            print(f"    {n:4d}  {b}")


# Local CSV fallback used ONLY by --dry-run so the production Google Sheet is
# never read or mutated during a heuristic run. Resolved relative to this file
# so it works regardless of the caller's working directory.
_DRY_RUN_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Copy of Road to Battlefield 2026 (Responses) - Form Responses 1 (1).csv",
)


def _read_csv_rows(path: str):
    """Read the local form-responses CSV as (header, rows) using csv.DictReader.

    Mirrors the shape returned by read_sheet_rows() — header is a list of
    column names and rows is a list of lists in header order — so the rest of
    run_batch needs no changes. Dry-run only; never hits the Sheets API.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [[row.get(col, "") for col in header] for row in reader]
    return header, rows


def run_batch(config: Config, *, dry_run: bool = False, force: bool = False, limit: int = 0):
    if dry_run:
        # Dry-run reads the local CSV copy only — never the production Sheet.
        header, rows = _read_csv_rows(_DRY_RUN_CSV)
        sheets_service = None
        source = f"local CSV (dry-run): {_DRY_RUN_CSV}"
    else:
        sheets_service = get_sheets_service(config.service_account_path)
        header, rows = read_sheet_rows(
            sheets_service, config.sheet_id, config.sheet_range
        )
        source = f"sheet {config.sheet_id} (tab {config.sheet_range!r})"
    if limit > 0:
        rows = rows[:limit]
    cols = _find_columns(header, config)
    print(f"Read {len(rows)} data rows from {source}", flush=True)
    print(
        f"Columns -> name: {cols['name']} | country: {cols['country']}",
        flush=True,
    )

    # Source-tab bookkeeping for per-row coloring. batchUpdate addresses tabs
    # by integer sheetId (not title), so resolve the source tab's sheetId once
    # here. header_row mirrors read_sheet_rows()'s default (1) -- data row i in
    # `rows` lives at 0-indexed sheet row header_row + i. Skipped in dry-run:
    # no sheets_service and no writes to the source tab.
    header_row = 1
    form_responses_sheet_id = None
    if not dry_run:
        form_responses_sheet_id = get_sheet_id_by_title(
            sheets_service, config.sheet_id, config.sheet_range
        )

    # Lazy import so --dry-run works without paying the ADK import cost and
    # so a missing/incompatible ADK install cannot break a heuristic run.
    workflow = None
    if not dry_run:
        from .adk_agents import AdkSorterWorkflow
        workflow = AdkSorterWorkflow(config.model)

    checkpoint = {} if dry_run else _load_checkpoint(config.checkpoint_path)

    grouped = defaultdict(list)   # bucket -> [(name, ts, incorporated_raw, country_raw)]
    other_log = []                # [(name, ts, incorporated_raw, country_raw, bucket)]
    errors = {}

    def _cell(row: list, idx: int) -> str:
        return (row[idx] if idx < len(row) else "").strip()

    def process(i: int, row: list):
        row_id = f"row_{i}"
        name = _cell(row, cols["name"])
        ts = _cell(row, cols["timestamp"])
        country_raw = _cell(row, cols["country"])
        # Optional column (may be absent in older sheets) -> emit "" then.
        incorporated_raw = (
            _cell(row, cols["incorporated"])
            if cols.get("incorporated") is not None
            else ""
        )
        if not dry_run and checkpoint.get(row_id) and not force:
            return row_id, checkpoint[row_id], name, ts, incorporated_raw, country_raw, None
        try:
            bucket = (
                deterministic_classify(country_raw)
                if dry_run
                else workflow.classify(country_raw)
            )
        except Exception as exc:  # noqa: BLE001 — isolate each row failure
            return row_id, None, name, ts, incorporated_raw, country_raw, f"{type(exc).__name__}: {exc}"
        return row_id, bucket, name, ts, incorporated_raw, country_raw, None

    total = len(rows)
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        futures = {pool.submit(process, i, row): i for i, row in enumerate(rows)}
        done = 0
        for fut in as_completed(futures):
            row_id, bucket, name, ts, incorporated_raw, country_raw, err = fut.result()
            done += 1
            if err:
                errors[row_id] = err
            elif bucket in TARGET_TABS:
                grouped[bucket].append((name, ts, incorporated_raw, country_raw))
            else:
                other_log.append((name, ts, incorporated_raw, country_raw, bucket))
            # Mark each classified row by coloring its startup-name cell
            # emerald green in the source "Form Responses 1" tab. Skipped in
            # dry-run (no sheet writes) and for errored rows. Non-fatal: a
            # coloring failure is logged but never aborts the batch.
            if not dry_run and not err and bucket is not None:
                try:
                    color_name_cell(
                        sheets_service,
                        config.sheet_id,
                        form_responses_sheet_id,
                        header_row + futures[fut],
                        cols["name"],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"WARNING: could not color cell for {row_id}: {exc}",
                        flush=True,
                    )
            if not dry_run:
                checkpoint[row_id] = bucket
            if done % 50 == 0 or done == total:
                mode = "dry" if dry_run else "adk"
                print(f"Progress [{mode}]: {done}/{total}", flush=True)

    if not dry_run:
        _save_checkpoint(config.checkpoint_path, checkpoint)

    # Write one tab per target country back into the SAME spreadsheet.
    # Dry-run never touches the production Sheet — the summary goes to stdout
    # only (via _print_summary), and no tabs are created or modified.
    _print_summary(grouped, other_log, total)
    if dry_run:
        print(
            "\nDry-run: skipped sheet writes — no tabs created or modified.",
            flush=True,
        )
    else:
        for bucket, title in TARGET_TABS.items():
            create_sheet_tab(sheets_service, config.sheet_id, title)
            write_tab_data(
                sheets_service, config.sheet_id, title, grouped.get(bucket, [])
            )
        print(f"\nWrote {len(TARGET_TABS)} tabs into sheet {config.sheet_id}", flush=True)
    return {
        "classified": total - len(errors),
        "errors": errors,
        "excluded": len(other_log),
        "tabs": {b: len(grouped.get(b, [])) for b in TARGET_TABS},
    }
