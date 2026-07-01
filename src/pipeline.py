"""
Batch driver: reads application rows from a Google Sheet, classifies each
startup country (of incorporation on the alchemist sheet, of physical HQ on
the r2b sheet) via the ADK classifier+verifier in a SINGLE batch (or a local
heuristic in --dry-run), groups rows into country buckets, and writes one
tab per target country back INTO THE SAME SHEET, carrying the full source
row (all columns) verbatim into every output tab.

Batch mode sends ALL uncheckpointed rows to classify_batch() in ONE call
(2-4 LLM calls total: classifier + verifier, plus an optional retry round on
the verifier-rejected subset) instead of one ADK call per row. For 687 rows
that is 2-4 calls, down from ~1374.
"""
import csv
import json
import os
import re
import unicodedata
from collections import OrderedDict, defaultdict

from .config import Config
from .google_clients import (
    color_cells_batch,
    create_sheet_tab,
    get_sheet_id_by_title,
    get_sheets_service,
    read_sheet_rows,
    write_tab_data,
)

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

COL_NEEDLES = {
    "timestamp": ["timestamp"],
    "incorporated": ["incorporated"],
}
# Display-only reference columns: a missing needle yields "" instead of
# crashing the whole batch -- same tolerance `incorporated` already has.
OPTIONAL_NEEDLES = {"incorporated", "founder", "email", "telegram", "pitch_deck", "dedup"}


def _norm(text: str) -> str:
    """Accent-folded lowercase for heuristic matching only."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(c)
    ).lower().strip()


def _find_columns(header: list, config: Config) -> dict:
    """Locate columns by case-insensitive substring; return 0-indexed positions."""
    needles = {
        **COL_NEEDLES,
        "name": [config.name_column],
        "country": [config.country_column],
        "founder": [config.founder_name_column],
        "email": [config.email_column],
        "telegram": [config.telegram_column],
        "pitch_deck": [config.pitch_deck_column],
        "dedup": [config.dedup_column],
    }
    lowered = [(_norm(h), i) for i, h in enumerate(header)]
    found = {}
    for key, key_needles in needles.items():
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
        found[key] = idx
    return found


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

# MENA countries get their own output tab. Matched against the normalized
# country_raw text with word boundaries so "oman" won't false-match
# "Romania" and "uae" won't false-match substrings.
MENA_SYNONYMS = [
    "qatar",
    "uae", "united arab emirates",
    "oman",
    "egypt",
    "algeria", "algerie",
    "jordan",
    "pakistan",
]


def _is_mena(country_raw: str) -> bool:
    """True if the country text names a MENA country (Qatar, UAE, Oman,
    Egypt, Algeria, Jordan, Pakistan)."""
    text = _norm(country_raw)
    if not text:
        return False
    return any(re.search(rf"\b{re.escape(syn)}\b", text) for syn in MENA_SYNONYMS)


def deterministic_classify(country_raw: str) -> str:
    """Local heuristic classifier -- only for --dry-run verification."""
    text = _norm(country_raw)
    if not text:
        return "Other"
    for city, bucket in _CITY_COUNTRY.items():
        if city in text:
            return bucket
    for syn, bucket in _COUNTRY_SYNONYMS.items():
        if re.search(rf"\b{re.escape(syn)}\b", text):
            return bucket
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


def _checkpoint_entry(value):
    """Normalize a checkpoint entry to (bucket, needs_review).

    Legacy checkpoints stored bare bucket strings; newer runs store
    {"bucket": ..., "needs_review": ...} dicts so a resumed run keeps
    ambiguous rows routed to Human Review.
    """
    if isinstance(value, dict):
        return (value.get("bucket") or "Other", bool(value.get("needs_review", False)))
    return (value or "Other", False)


def _route_rows(row_meta, buckets, errored_indices, *, dry_run, header_row, name_col):
    """Group classified rows into country buckets, a Human Review list, the
    MENA list, and the Other/excluded log. Returns (grouped, review_rows,
    mena_log, other_log, green_coords, red_coords).

    Each entry in `row_meta` is a 3-tuple:
    (i, country_raw, full_row). `full_row` is the complete source row (all
    columns) copied verbatim into output tabs — one row per startup, every
    field. Output rows (grouped, review, mena_log, other_log) carry the full
    source row; other_log appends the classifier bucket as a trailing cell
    (stripped before writing).

    Rows whose classifier flagged needs_review=True go to the Human Review list
    AND are marked RED in the source tab so an operator can spot rows awaiting
    sign-off. Every other row keeps the prior behavior: bucket rows, MENA rows,
    and Other rows are marked emerald green in the source tab when not in
    dry-run and not errored.

    Non-target rows whose country_raw matches a MENA country (Qatar, UAE, Oman,
    Egypt, Algeria, Jordan, Pakistan) are routed to mena_log instead of
    other_log, regardless of the classifier's bucket label, so they land in the
    dedicated MENA tab. other_log keeps only non-target, non-MENA countries.
    """
    grouped = defaultdict(list)
    review_rows = []
    mena_log = []
    other_log = []
    green_coords = []
    red_coords = []
    for (i, country_raw, full_row) in row_meta:
        entry = buckets[i]
        if entry is None:
            bucket, needs_review = "Other", False
        else:
            bucket, needs_review = entry
        out_row = list(full_row)
        if needs_review:
            review_rows.append(out_row)
            if not dry_run and i not in errored_indices:
                red_coords.append((header_row + i, name_col))
            continue
        if bucket in TARGET_TABS:
            grouped[bucket].append(out_row)
        elif _is_mena(country_raw):
            mena_log.append(out_row)
        else:
            other_log.append(out_row + [bucket])
        if not dry_run and i not in errored_indices:
            green_coords.append((header_row + i, name_col))
    return grouped, review_rows, mena_log, other_log, green_coords, red_coords


def _print_summary(grouped: dict, other_log: list, review_rows: list, total: int, mena_log: list) -> None:
    print("\n=== Summary ===", flush=True)
    placed = 0
    for bucket in TARGET_TABS:
        n = len(grouped.get(bucket, []))
        placed += n
        print(f"  {bucket:32s} {n}")
    print(f"  {'Human Review':32s} {len(review_rows)}")
    print(f"  {'MENA':32s} {len(mena_log)}")
    print(f"  {'(excluded / Other)':32s} {len(other_log)}")
    print(f"  {'TOTAL rows':32s} {total}")
    if other_log:
        from collections import Counter
        buckets = Counter(t[-1] for t in other_log)
        print("\nExcluded breakdown (top):")
        for b, n in buckets.most_common(15):
            print(f"    {n:4d}  {b}")


_DRY_RUN_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Copy of Road to Battlefield 2026 (Responses) - Form Responses 1 (1).csv",
)


def _read_csv_rows(path: str):
    """Read the local form-responses CSV as (header, rows). Dry-run only."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [[row.get(col, "") for col in header] for row in reader]
    return header, rows


def _deduplicate_rows(row_meta: list, rows: list, dedup_col_idx, *, column_name: str) -> list:
    """Drop duplicate rows by DEDUP_COLUMN value.

    Matching is case-insensitive with whitespace stripped. Empty/null values
    are NOT duplicates (every empty-dedup row is kept). Only the first
    occurrence of each non-empty value is kept; later duplicates are dropped
    entirely and excluded from classification and output.

    `row_meta[i]` carries the original source row index in tuple position 0;
    that index is preserved so source-cell coloring still maps to the right
    row. `rows` is the raw source rows list so we can read the dedup cell
    directly via `dedup_col_idx`.
    """
    if not column_name or dedup_col_idx is None:
        return row_meta
    seen: set = set()
    unique: list = []
    removed = 0
    for meta_entry in row_meta:
        i = meta_entry[0]
        raw = ""
        if 0 <= dedup_col_idx < len(rows[i]):
            raw = (rows[i][dedup_col_idx] or "").strip()
        key = raw.lower()
        if not key:
            # Empty / null -> never a duplicate; always keep.
            unique.append(meta_entry)
            continue
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        unique.append(meta_entry)
    total = len(row_meta)
    print(
        f"Found {removed} duplicates in column '{column_name}', removing...",
        flush=True,
    )
    print(
        f"Deduplication: removed {removed} of {total} rows "
        f"({len(unique)} unique remaining).",
        flush=True,
    )
    return unique


def run_batch(config: Config, *, dry_run: bool = False, force: bool = False, limit: int = 0):
    if dry_run:
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
        "Columns -> "
        f"name: {cols['name']} | country: {cols['country']} | "
        f"founder: {cols.get('founder')} | email: {cols.get('email')} | "
        f"telegram: {cols.get('telegram')} | pitch_deck: {cols.get('pitch_deck')}",
        flush=True,
    )

    header_row = 1
    form_responses_sheet_id = None
    if not dry_run:
        form_responses_sheet_id = get_sheet_id_by_title(
            sheets_service, config.sheet_id, config.sheet_range
        )

    workflow = None
    if not dry_run:
        from .adk_agents import AdkSorterWorkflow
        workflow = AdkSorterWorkflow(config.model)

    checkpoint = {} if dry_run else _load_checkpoint(config.checkpoint_path)

    # Collect per-row fields up front so the whole batch can be classified in
    # a few LLM calls instead of one call per row.
    # 3-tuple: (i, country_raw, full_row). `full_row` is the complete source
    # row (all columns) copied verbatim into output tabs — one row per startup,
    # every field. The source index `i` is preserved so source-cell coloring
    # still maps correctly.
    def _cell(row: list, idx: int) -> str:
        return (row[idx] if idx is not None and idx < len(row) else "").strip()

    row_meta = []
    for i, row in enumerate(rows):
        country_raw = _cell(row, cols["country"])
        row_meta.append((i, country_raw, list(row)))

    source_row_count = len(row_meta)

    # Deduplicate BEFORE classification: drop later rows whose DEDUP_COLUMN
    # value matches an earlier row (case-insensitive, whitespace stripped).
    # Empty values are NOT duplicates. Duplicates never reach the LLM and are
    # never written to output tabs. Original source row index `i` is preserved
    # in each tuple so source-cell coloring still maps correctly.
    dedup_col_idx = cols.get("dedup")
    row_meta = _deduplicate_rows(
        row_meta, rows, dedup_col_idx, column_name=config.dedup_column
    )
    total = len(row_meta)  # post-dedup count, used for reporting/summary

    # buckets[i] = (bucket, needs_review) for row index i (None until set).
    # Sized by the SOURCE row count (pre-dedup): row_meta entries keep their
    # original source index `i` (up to source_row_count-1) so cell coloring
    # still maps to the correct source row.
    buckets = [None] * source_row_count
    to_classify = []  # list of (row_index, {"row_id": row_index, "country_raw": ...})
    for (i, country_raw, _full_row) in row_meta:
        row_id = f"row_{i}"
        if not dry_run and checkpoint.get(row_id) and not force:
            buckets[i] = _checkpoint_entry(checkpoint[row_id])
        else:
            to_classify.append((i, {"row_id": i, "country_raw": country_raw}))

    errors = {}

    if dry_run:
        for (i, _item) in to_classify:
            # Find this row's meta entry by source index i (row_meta is
            # filtered post-dedup, so we can't index it by i directly).
            meta = next((m for m in row_meta if m[0] == i), None)
            country_raw = meta[1] if meta else ""
            try:
                buckets[i] = (deterministic_classify(country_raw), False)
            except Exception as exc:
                errors[f"row_{i}"] = f"{type(exc).__name__}: {exc}"
                buckets[i] = ("Other", False)
        print(f"Classified {len(to_classify)} rows (dry-run heuristic).", flush=True)
    else:
        # Single batch: ALL unclassified rows in one classify_batch() call
        # (2-4 LLM calls total: classifier+verifier, plus an optional retry
        # round on the verifier-rejected subset). No chunking and no
        # ThreadPoolExecutor -- 2-4 calls need no parallelism.
        batch_items = [item for (_i, item) in to_classify]
        print(f"Batch mode: {len(to_classify)} rows in a single batch (2-4 LLM calls total).", flush=True)
        try:
            batch_buckets = workflow.classify_batch(batch_items)
        except Exception as exc:
            for (i, _item) in to_classify:
                errors[f"row_{i}"] = f"batch: {type(exc).__name__}: {exc}"
                buckets[i] = ("Other", False)
            print(f"Batch classify FAILED ({len(to_classify)} rows) -- {type(exc).__name__}: {exc}", flush=True)
        else:
            for idx, (i, _item) in enumerate(to_classify):
                entry = batch_buckets[idx] if idx < len(batch_buckets) else ("Other", False)
                buckets[i] = entry
                checkpoint[f"row_{i}"] = {"bucket": entry[0], "needs_review": entry[1]}
            _save_checkpoint(config.checkpoint_path, checkpoint)
            print(f"Batch classify done ({len(to_classify)} rows).", flush=True)

    errored_indices = set()
    for k in errors:
        try:
            errored_indices.add(int(k.split("_", 1)[1]))
        except (IndexError, ValueError):
            pass

    grouped, review_rows, mena_log, other_log, green_coords, red_coords = _route_rows(
        row_meta, buckets, errored_indices,
        dry_run=dry_run, header_row=header_row, name_col=cols["name"],
    )

    _print_summary(grouped, other_log, review_rows, total, mena_log)
    if dry_run:
        print("\nDry-run: skipped sheet writes -- no tabs created or modified.", flush=True)
    else:
        if green_coords or red_coords:
            try:
                color_cells_batch(sheets_service, config.sheet_id, form_responses_sheet_id, green_coords, red_coords)
            except Exception as exc:
                print(f"WARNING: batch cell coloring failed ({len(green_coords)} green, {len(red_coords)} red cells): {exc}", flush=True)
        # Country buckets are colored green (finalized); Human Review is not.
        review_tab = "Human Review"
        mena_tab = "MENA"
        other_tab = "Other Countries"
        out_header = list(config.output_columns)
        tab_writes = [
            (title, grouped.get(bucket, []), False)
            for bucket, title in TARGET_TABS.items()
        ]
        tab_writes.append((review_tab, review_rows, False))
        # MENA countries (Qatar, UAE, Oman, Egypt, Algeria, Jordan, Pakistan)
        # get their own visible tab. mena_log rows carry the full source row
        # (all columns) -- same format as every other tab.
        tab_writes.append((mena_tab, mena_log, False))
        # Non-target, non-MENA countries (Canada, China, Japan, etc.) get
        # their own visible tab. other_log rows carry the full source row plus
        # a trailing bucket cell; strip the bucket for write_tab_data, which
        # expects the same full-row format as every other tab.
        tab_writes.append((other_tab, [r[:-1] for r in other_log], False))
        for title, rows, color in tab_writes:
            create_sheet_tab(sheets_service, config.sheet_id, title)
            write_tab_data(
                sheets_service, config.sheet_id, title, rows,
                color=color, header=out_header,
            )
        print(f"\nWrote {len(tab_writes)} tabs into sheet {config.sheet_id}", flush=True)
    return {
        "classified": total - len(errors),
        "errors": errors,
        "excluded": len(other_log),
        "mena": len(mena_log),
        "review": len(review_rows),
        "tabs": {b: len(grouped.get(b, [])) for b in TARGET_TABS},
    }
