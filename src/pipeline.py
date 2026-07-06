"""
Batch driver: reads application rows from a Google Sheet, classifies each
startup's country of incorporation via the ADK classifier+verifier in a
SINGLE batch (or a local heuristic in --dry-run), groups rows into country
buckets, and writes one tab per target country back INTO THE SAME SHEET,
carrying the full source row (all 16 columns) verbatim into every output tab.

Batch mode sends ALL uncheckpointed rows to classify_batch() in ONE call
(2-4 LLM calls total: classifier + verifier, plus an optional retry round on
the verifier-rejected subset) instead of one ADK call per row. For 687 rows
that is 2-4 calls, down from ~1374.
"""
import json
import os
import re
import unicodedata
from collections import OrderedDict, defaultdict

from .config import Config
from .google_clients import (
    color_cells_batch,
    create_sheet_tab,
    delete_sheet_tab,
    get_sheet_id_by_title,
    get_sheets_service,
    list_existing_tab_titles,
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

# Tabs that the algorithm can create. Used by _cleanup_stale_tabs to
# determine which tabs are safe to delete: ONLY tabs in this set can
# be deleted. Any tab NOT in this set (CRM, Form Responses 1, custom
# tabs, etc.) is NEVER deleted.
ALGORITHM_TABS = set(TARGET_TABS.keys()) | {"Human Review", "MENA", "Other Countries"}


def _is_algorithm_tab(title: str) -> bool:
    """Case-insensitive check with whitespace stripping."""
    clean = title.strip().lower()
    return clean in {t.lower() for t in ALGORITHM_TABS}

COL_NEEDLES = {}  # name/country/founder/etc are added dynamically in _find_columns
# Display-only reference columns: a missing needle yields "" instead of
# crashing the whole batch.
OPTIONAL_NEEDLES = {"founder", "email", "telegram", "pitch_deck", "dedup"}


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
    }
    # Only add dedup needle if a column name is configured; an empty string
    # needle would match every header ("" is a substring of anything).
    if config.dedup_column:
        needles["dedup"] = [config.dedup_column]
    lowered = [(_norm(h), i) for i, h in enumerate(header)]
    found = {}
    for key, key_needles in needles.items():
        norm_needles = [_norm(n) for n in key_needles if _norm(n)]
        if not norm_needles:
            found[key] = None
            continue
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
    # Cyrillic spellings
    "тошкент": "Uzbekistan", "ташкент": "Uzbekistan", "самарканд": "Uzbekistan",
    "андижан": "Uzbekistan", "наманган": "Uzbekistan", "фергана": "Uzbekistan",
    "нукус": "Uzbekistan", "каракалпакстан": "Uzbekistan",
    "astana": "Kazakhstan", "almaty": "Kazakhstan", "karaganda": "Kazakhstan",
    "uralsk": "Kazakhstan", "petropavlovsk": "Kazakhstan",
    "shymkent": "Kazakhstan", "aktobe": "Kazakhstan", "pavlodar": "Kazakhstan",
    "oskemen": "Kazakhstan", "atyrau": "Kazakhstan",
    # Cyrillic spellings
    "астана": "Kazakhstan", "алматы": "Kazakhstan", "караганда": "Kazakhstan",
    "петропавловск": "Kazakhstan", "шымкент": "Kazakhstan", "актобе": "Kazakhstan",
    "павлодар": "Kazakhstan", "өскемен": "Kazakhstan", "оскемен": "Kazakhstan",
    "bishkek": "Kyrgyzstan",
    # Cyrillic spellings
    "бишкек": "Kyrgyzstan",
    "tbilisi": "Georgia",
    # Cyrillic spellings
    "тбилиси": "Georgia",
    "baku": "Azerbaijan",
    # Cyrillic spellings
    "баку": "Azerbaijan",
    "istanbul": "Turkiye", "ankara": "Turkiye", "izmir": "Turkiye",
    # Cyrillic spellings
    "стамбул": "Turkiye", "анкара": "Turkiye", "измир": "Turkiye",
    "ulaanbaatar": "Mong. Turkmenistan Tajikistan",
    "dushanbe": "Mong. Turkmenistan Tajikistan",
    "ashgabat": "Mong. Turkmenistan Tajikistan",
    # Cyrillic spellings
    "душанбе": "Mong. Turkmenistan Tajikistan",
    "ашхабад": "Mong. Turkmenistan Tajikistan",
}
_COUNTRY_SYNONYMS = {
    "uzbekistan": "Uzbekistan", "uzbekiston": "Uzbekistan", "ozbekiston": "Uzbekistan",
    "узбекистан": "Uzbekistan", "ўзбекистон": "Uzbekistan",
    "kazakhstan": "Kazakhstan", "kazahstan": "Kazakhstan", "kazakshtan": "Kazakhstan",
    "казахстан": "Kazakhstan", "қазақстан": "Kazakhstan",
    "kyrgyzstan": "Kyrgyzstan", "kyrgyz republic": "Kyrgyzstan",
    "кыргызстан": "Kyrgyzstan", "кыргыз республикасы": "Kyrgyzstan",
    "georgia": "Georgia",
    "грузия": "Georgia",
    "azerbaijan": "Azerbaijan",
    "азербайджан": "Azerbaijan",
    "turkiye": "Turkiye", "turkey": "Turkiye",
    "турция": "Turkiye", "турkiye": "Turkiye",
    "united states": "USA", "united states of america": "USA", "san francisco": "USA",
    "new york": "USA", "chicago": "USA", "boston": "USA", "seattle": "USA",
    "austin": "USA", "miami": "USA", "los angeles": "USA", "silicon valley": "USA",
    "denver": "USA", "dallas": "USA", "atlanta": "USA", "washington": "USA",
    "mongolia": "Mong. Turkmenistan Tajikistan",
    "turkmenistan": "Mong. Turkmenistan Tajikistan",
    "tajikistan": "Mong. Turkmenistan Tajikistan",
    "монголия": "Mong. Turkmenistan Tajikistan",
    "туркменистан": "Mong. Turkmenistan Tajikistan",
    "таджикистан": "Mong. Turkmenistan Tajikistan",
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
    """Local heuristic classifier -- only for --dry-run verification.

    Implements first-mentioned-wins: scans the text left-to-right and returns
    the bucket of the FIRST matching city or country synonym. This mirrors the
    LLM classifier's Rule 1 (first-mentioned target-bucket country wins).
    Cyrillic city/country spellings are included so the heuristic handles
    the same messy multi-script input the LLM does.
    """
    text = _norm(country_raw)
    if not text:
        return "Other"
    # Build a combined list of (synonym, bucket) pairs, then find the
    # earliest match position in the text. First-mentioned wins.
    candidates = []
    for city, bucket in _CITY_COUNTRY.items():
        pos = text.find(city)
        if pos >= 0:
            candidates.append((pos, bucket))
    for syn, bucket in _COUNTRY_SYNONYMS.items():
        m = re.search(rf"\b{re.escape(syn)}\b", text)
        if m:
            candidates.append((m.start(), bucket))
    # Token-level checks for short abbreviations that word-boundary regex
    # would miss (e.g. "US/KZ" -> token "us" -> USA).
    tokens = set(text.replace("/", " ").replace(",", " ").split())
    if "usa" in tokens or "us" in tokens:
        # Find position of first "us"/"usa" token for ordering.
        for tok in ("usa", "us"):
            pos = text.find(tok)
            if pos >= 0:
                candidates.append((pos, "USA"))
                break
    if "kz" in tokens:
        pos = text.find("kz")
        if pos >= 0:
            candidates.append((pos, "Kazakhstan"))
    if candidates:
        # Sort by position; first-mentioned wins.
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]
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


def _route_rows(
    row_meta, buckets, errored_indices, *,
    dry_run, header_row, name_col,
    target_tabs=None, mena_enabled=True,
):
    """Group classified rows into country buckets, a Human Review list, the
    MENA list, and the Other/excluded log. Returns (grouped, review_rows,
    mena_log, other_log, green_coords, red_coords).

    Each entry in `row_meta` is a 3-tuple:
    (i, country_raw, full_row). `full_row` is the complete source row (all
    16 columns) copied verbatim into output tabs -- one row per startup, every
    field. Output rows (grouped, mena_log, other_log) carry the full source
    row; other_log appends the classifier bucket as a trailing cell
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

    ``target_tabs`` overrides the module-level TARGET_TABS (used by the
    dashboard to let the user deselect countries). When None, the module
    default is used. ``mena_enabled=False`` forces all rows to other_log
    (no MENA tab).
    """
    if target_tabs is None:
        target_tabs = TARGET_TABS
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
        if bucket in target_tabs:
            grouped[bucket].append(out_row)
        elif mena_enabled and _is_mena(country_raw):
            mena_log.append(out_row)
        else:
            other_log.append(out_row + [bucket])
        if not dry_run and i not in errored_indices:
            green_coords.append((header_row + i, name_col))
    return grouped, review_rows, mena_log, other_log, green_coords, red_coords


def _print_summary(grouped: dict, other_log: list, review_rows: list, total: int, mena_log: list,
                   target_tabs=None) -> None:
    if target_tabs is None:
        target_tabs = TARGET_TABS
    print("\n=== Summary ===", flush=True)
    placed = 0
    for bucket in target_tabs:
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


def _normalize_for_fuzzy(text: str) -> str:
    """Aggressive normalization for fuzzy name comparison.

    Lowercase, strip accents, remove ALL punctuation AND whitespace so
    'Agro ai', 'AgroAi', 'Agro.ai', and 'Agro  ai' all reduce to 'agroai'.
    Deliberately simple (no edit distance) to avoid false-positive
    over-merging like 'YerAI' vs 'CERTI'.
    """
    if not text:
        return ""
    # Accent-folded lowercase.
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    ).lower()
    # Drop punctuation AND whitespace entirely so spacing/punctuation
    # variations collapse to the same key.
    return re.sub(r"[^\w]", "", folded, flags=re.UNICODE)


def _deduplicate_rows(
    row_meta: list,
    rows: list,
    dedup_col_idx,
    *,
    column_name: str,
    name_col_idx=None,
) -> list:
    """Drop duplicate rows by DEDUP_COLUMN value, then fuzzy name.

    Two passes (in order), each only dropping a row if it matches an
    already-kept row from an EARLIER pass:

    1. EXACT match on the raw dedup cell (case-insensitive, whitespace
       stripped) -- same as the original behavior.
    2. FUZZY name match: normalize names (strip punctuation, collapse
       whitespace, lowercase, accent-fold) and drop later rows whose
       normalized name matches an earlier kept row.

    Email is intentionally NOT used for deduplication: a founder may
    legitimately submit multiple distinct startups (e.g. RUNA and QORGAN
    from the same email). Deduping on email was silently dropping real
    distinct entries, so that pass was removed.

    Empty/null values are NEVER a duplicate (every empty row is kept).
    Only the first occurrence of each value is kept; later duplicates are
    dropped entirely and excluded from classification and output. Each
    removed duplicate is logged with the reason ('exact', 'fuzzy').

    `row_meta[i]` carries the original source row index in tuple position 0;
    that index is preserved so source-cell coloring still maps to the right
    row. `rows` is the raw source rows list so we can read cells directly
    via their column index.

    `name_col_idx` is an optional column index used by the fuzzy pass.
    When None, the fuzzy pass falls back to the dedup column. If the dedup
    column has no usable name, only exact dedup runs.
    """
    if not column_name or dedup_col_idx is None:
        return row_meta

    def _cell_val(idx: int, row_idx: int) -> str:
        if idx is None or row_idx >= len(rows):
            return ""
        if 0 <= idx < len(rows[row_idx]):
            return (rows[row_idx][idx] or "").strip()
        return ""

    total = len(row_meta)
    removed = {"exact": 0, "fuzzy": 0}
    # Keep LATEST duplicate: iterate in reverse so later rows overwrite
    # earlier ones in the seen sets. Then reverse back to original order.
    kept_reversed: list = []
    seen_exact: set = set()
    seen_fuzzy_names: set = set()

    for meta_entry in reversed(row_meta):
        i = meta_entry[0]
        raw = _cell_val(dedup_col_idx, i)
        exact_key = raw.lower()
        if not exact_key:
            # Empty dedup value -> never an exact duplicate; keep this row
            exact_key = None
        # PASS 1: exact dedup
        if exact_key is not None and exact_key in seen_exact:
            removed["exact"] += 1
            print(
                f"  dedup-exact: row {i} dropped (duplicate of LATER row "
                f"on '{column_name}': {raw!r})",
                flush=True,
            )
            continue
        if exact_key is not None:
            seen_exact.add(exact_key)
        # PASS 2: fuzzy name dedup (name column defaults to the dedup column)
        name_col = name_col_idx if name_col_idx is not None else dedup_col_idx
        name_val = _cell_val(name_col, i)
        fuzzy_key = _normalize_for_fuzzy(name_val)
        if fuzzy_key:
            if fuzzy_key in seen_fuzzy_names:
                removed["fuzzy"] += 1
                print(
                    f"  dedup-fuzzy: row {i} dropped (normalized name "
                    f"{fuzzy_key!r} matches a LATER row)",
                    flush=True,
                )
                continue
            seen_fuzzy_names.add(fuzzy_key)
        kept_reversed.append(meta_entry)

    kept = list(reversed(kept_reversed))

    total_removed = sum(removed.values())
    print(
        f"Found {total_removed} duplicates in column '{column_name}' "
        f"(exact={removed['exact']}, fuzzy={removed['fuzzy']}), removing...",
        flush=True,
    )
    print(
        f"Deduplication: removed {total_removed} of {total} rows "
        f"({len(kept)} unique remaining).",
        flush=True,
    )
    return kept


def _llm_semantic_dedup(
    row_meta: list,
    rows: list,
    name_col_idx,
    *,
    workflow,
) -> list:
    """Second dedup pass: LLM checks surviving startup names semantically.

    Runs AFTER the string-based pass (exact + fuzzy), which already caught
    punctuation/case/whitespace variants. This pass catches semantic
    duplicates the string pass missed -- e.g. 'RUNA Tech' vs 'RUNA
    Technology' vs 'RUNA' -- by asking Gemini to group names referring to
    the same startup.

    A SINGLE LLM call is made for the whole batch of surviving names (chunked
    only if the list exceeds 500 names). For each group with >1 entry, the
    LATEST submission (highest source row index) is kept and earlier entries
    are dropped, mirroring the string pass's keep-latest semantics. Each
    removed row is logged with reason 'semantic'.

    Conservative: any LLM error, empty result, or disabled flag means this
    pass returns row_meta unchanged (keep everything). A false positive
    (dropping a real distinct startup) is worse than a false negative
    (keeping a duplicate).

    `name_col_idx` is the column to read startup names from. `workflow` is
    an AdkSorterWorkflow instance with `.dedup_names_batch()`.
    """
    if not row_meta or name_col_idx is None:
        return row_meta

    def _cell(idx: int, row_idx: int) -> str:
        if idx is None or row_idx >= len(rows):
            return ""
        if 0 <= idx < len(rows[row_idx]):
            return (rows[row_idx][idx] or "").strip()
        return ""

    # Map each distinct startup name -> list of row_meta entries that carry
    # it. row_meta entries are 3-tuples (i, country_raw, full_row); source
    # index i sorts ascending, so the LAST entry for a name is the latest
    # submission.
    name_to_entries: "OrderedDict[str, list]" = OrderedDict()
    ordered_names: list[str] = []
    for meta_entry in row_meta:
        i = meta_entry[0]
        name = _cell(name_col_idx, i)
        if not name:
            continue
        if name not in name_to_entries:
            name_to_entries[name] = []
            ordered_names.append(name)
        name_to_entries[name].append(meta_entry)

    if len(ordered_names) < 2:
        return row_meta  # nothing to compare

    print(
        f"LLM semantic dedup: checking {len(ordered_names)} unique startup "
        f"names in a single batch...",
        flush=True,
    )
    try:
        groups = workflow.dedup_names_batch(ordered_names)
    except Exception as exc:  # noqa: BLE001 - never drop startups on LLM failure
        print(
            f"LLM semantic dedup FAILED ({type(exc).__name__}: {exc}); "
            f"skipping (keeping all rows).",
            flush=True,
        )
        return row_meta

    if not groups:
        print("LLM semantic dedup: no semantic duplicates found.", flush=True)
        return row_meta

    # For each group, the entry with the highest source index i is kept;
    # all earlier entries for that name are dropped. We track the id() of
    # the meta_entry tuples to REMOVE (not keep) -- tuple identity lets us
    # mark specific instances even when two rows share the same name.
    drop_entry_ids: set = set()
    dropped = 0
    for group_names in groups:
        # Gather all meta entries whose name is in this group, in source
        # order (ascending i). id() lets us mark specific tuple instances.
        entries_in_group: list = []
        for n in group_names:
            for meta_entry in name_to_entries.get(n, []):
                entries_in_group.append(meta_entry)
        if len(entries_in_group) < 2:
            continue
        # Sort by source row index ascending; keep the LAST (latest).
        entries_in_group.sort(key=lambda e: e[0])
        keep_entry = entries_in_group[-1]
        for meta_entry in entries_in_group[:-1]:
            drop_entry_ids.add(id(meta_entry))
            dropped += 1
            i = meta_entry[0]
            keep_i = keep_entry[0]
            print(
                f"  dedup-semantic: row {i} dropped (same startup as row "
                f"{keep_i} per LLM; names: {group_names!r})",
                flush=True,
            )

    if dropped == 0:
        print("LLM semantic dedup: no rows dropped.", flush=True)
        return row_meta

    kept = [e for e in row_meta if id(e) not in drop_entry_ids]
    print(
        f"LLM semantic dedup: removed {dropped} semantic duplicates "
        f"({len(kept)} unique remaining).",
        flush=True,
    )
    return kept


def _cleanup_stale_tabs(sheets_service, sheet_id: str, new_tab_titles: list) -> list:
    """Delete tabs that exist in the sheet but are NOT in this run's tab list.

    Prevents stale country tabs from a prior run lingering with old data.
    Only ALGORITHM_TABS (country output tabs) can be deleted
    are never deleted. Returns the list of deleted tab titles (for logging).

    Idempotent and safe: each delete is a separate batchUpdate, a transient
    failure on one tab does not block the others, and the function logs
    every deletion so an operator can see what was removed.
    """
    existing = list_existing_tab_titles(sheets_service, sheet_id)
    new_set = set(new_tab_titles)
    to_delete = [
        title for title in existing
        if title not in new_set and _is_algorithm_tab(title)
    ]
    if not to_delete:
        return []
    deleted = []
    for title in to_delete:
        try:
            delete_sheet_tab(sheets_service, sheet_id, title)
            deleted.append(title)
            print(f"  tab-cleanup: deleted stale tab {title!r}", flush=True)
        except Exception as exc:
            print(
                f"WARNING: tab-cleanup: could not delete stale tab "
                f"{title!r}: {type(exc).__name__}: {exc}",
                flush=True,
            )
    return deleted


def run_batch(config: Config, *, dry_run: bool = False, force: bool = False, limit: int = 0,
              target_tabs=None, mena_enabled: bool = True):
    # Dry-run reads from the same Google Sheet as a normal run (the legacy
    # local-CSV path pointed at a file that no longer exists). Dry-run still
    # skips sheet writes and uses the local heuristic classifier instead of
    # the LLM, so it costs no API spend.
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
    form_responses_sheet_id = get_sheet_id_by_title(
        sheets_service, config.sheet_id, config.sheet_range
    )
    if form_responses_sheet_id is None:
        print(
            f"WARNING: source tab '{config.sheet_range}' not found in sheet "
            f"{config.sheet_id} — cell coloring will be skipped.",
            flush=True,
        )

    workflow = None
    if not dry_run:
        from .adk_agents import AdkSorterWorkflow
        workflow = AdkSorterWorkflow(config.model, config.country_column)

    checkpoint = {} if dry_run else _load_checkpoint(config.checkpoint_path)

    def _cell(row: list, idx: int) -> str:
        return (row[idx] if idx < len(row) else "").strip()

    # Collect per-row fields up front so the whole batch can be classified in
    # a few LLM calls instead of one call per row.
    # 3-tuple: (i, country_raw, full_row). `full_row` is the complete source
    # row (all 16 columns) copied verbatim into output tabs -- one row per
    # startup, every field. The source index `i` is preserved so source-cell
    # coloring still maps correctly.
    #
    # Blank-row gate: skip rows whose Startup Name cell is empty. Fully blank
    # rows (no startup name, no data) used to flow through to the LLM, get
    # misclassified as a target country, land in an output tab, and get colored
    # green -- all because nothing checked "is this actually a startup?".
    # Filtering here prevents blank rows from ever reaching classification,
    # routing, output tabs, or source-cell coloring.
    blank_skipped = 0
    row_meta = []
    for i, row in enumerate(rows):
        startup_name = _cell(row, cols["name"])
        if not startup_name:
            blank_skipped += 1
            continue
        country_raw = _cell(row, cols["country"])
        row_meta.append((i, country_raw, list(row)))
    if blank_skipped:
        print(
            f"Skipped {blank_skipped} blank rows (empty Startup Name) "
            f"before classification.",
            flush=True,
        )

    # Size buckets by the ORIGINAL source row count, not the filtered
    # row_meta length. buckets[i] is indexed by the source row index i
    # from enumerate(rows), which can exceed len(row_meta) when blank
    # rows are skipped. Blank rows' buckets[i] stay None and are never
    # accessed because they never reach classification or routing.
    source_row_count = len(rows)

    # Deduplicate BEFORE classification: drop later rows whose DEDUP_COLUMN
    # value matches an earlier row (case-insensitive, whitespace stripped).
    # Empty values are NOT duplicates. Duplicates never reach the LLM and are
    # never written to output tabs. Original source row index `i` is preserved
    # in each tuple so source-cell coloring still maps correctly.
    dedup_col_idx = cols.get("dedup")
    row_meta = _deduplicate_rows(
        row_meta, rows, dedup_col_idx,
        column_name=config.dedup_column,
        name_col_idx=cols.get("name"),
    )

    # Second dedup pass: LLM semantic dedup. Runs AFTER the string pass
    # (exact + fuzzy) so we only pay for one LLM call on the surviving
    # (string-unique) names. Catches semantic duplicates the string pass
    # misses ('RUNA Tech' vs 'RUNA Technology' vs 'RUNA'). Skipped in
    # dry-run (no LLM) and when LLM_DEDUP_ENABLED is false. Any LLM failure
    # is non-fatal: the pass keeps all rows and logs a warning.
    if not dry_run and config.llm_dedup_enabled and workflow is not None and row_meta:
        row_meta = _llm_semantic_dedup(
            row_meta, rows, cols.get("name"),
            workflow=workflow,
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
        for (i, item) in to_classify:
            country_raw = item["country_raw"]
            try:
                buckets[i] = (deterministic_classify(country_raw), False)
            except Exception as exc:
                errors[f"row_{i}"] = f"{type(exc).__name__}: {exc}"
                buckets[i] = ("Other", False)
        print(f"Classified {len(to_classify)} rows (dry-run heuristic).", flush=True)
    else:
        # Single batch: ALL unclassified rows in one classify_batch() call
        # (2-4 LLM calls per chunk of CHUNK_SIZE rows). Chunks run with
        # per-chunk retry (3 attempts, 5s/10s/20s backoff); a chunk that
        # fails all retries is marked 'Other' while the rest continue.
        # After each successful chunk the checkpoint is saved so a mid-batch
        # crash does not re-pay for already-classified chunks.
        batch_items = [item for (_i, item) in to_classify]
        print(f"Batch mode: {len(to_classify)} rows in a single batch (2-4 LLM calls per chunk).", flush=True)
        # Map row_id -> source index i so the checkpoint callback can update
        # buckets[i] and checkpoint[row_i] per successfully classified chunk.
        rid_to_i = {item["row_id"]: i for (i, item) in to_classify}

        def _on_chunk_done(merged: dict) -> None:
            for rid, entry in merged.items():
                idx = rid_to_i.get(rid)
                if idx is None:
                    continue
                buckets[idx] = entry
                checkpoint[f"row_{idx}"] = {"bucket": entry[0], "needs_review": entry[1]}
            _save_checkpoint(config.checkpoint_path, checkpoint)

        try:
            batch_buckets, failed_rids = workflow.classify_batch(batch_items, on_chunk_done=_on_chunk_done)
        except Exception as exc:
            # Only overwrite rows NOT already classified by on_chunk_done.
            # buckets[i] is None until set by the callback; ("Other", False)
            # is a tuple, so `is None` distinguishes "not classified" from
            # "classified as Other". This prevents a mid-batch crash after
            # partial success from clobbering already-checkpointed results.
            failed = 0
            for (i, _item) in to_classify:
                if buckets[i] is None:
                    errors[f"row_{i}"] = f"batch: {type(exc).__name__}: {exc}"
                    buckets[i] = ("Other", False)
                    failed += 1
            print(f"Batch classify FAILED ({failed} unclassified rows marked Other; "
                  f"{len(to_classify) - failed} already classified by earlier chunks) -- "
                  f"{type(exc).__name__}: {exc}", flush=True)
        else:
            for idx, (i, _item) in enumerate(to_classify):
                entry = batch_buckets[idx] if idx < len(batch_buckets) else ("Other", False)
                buckets[i] = entry
                checkpoint[f"row_{i}"] = {"bucket": entry[0], "needs_review": entry[1]}
            _save_checkpoint(config.checkpoint_path, checkpoint)
            # Surface rows whose chunk exhausted all retries (returned 'Other'
            # but not because the LLM said so) in the errors dict so the final
            # CLI report and exit code reflect the partial failure.
            rid_to_i = {item["row_id"]: i for (i, item) in to_classify}
            for rid in failed_rids:
                idx = rid_to_i.get(rid)
                if idx is not None:
                    errors[f"row_{idx}"] = "chunk exhausted all retries; marked Other"
            if failed_rids:
                print(f"Batch classify done ({len(to_classify)} rows; "
                      f"{len(failed_rids)} rows in failed chunk(s) marked Other).", flush=True)
            else:
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
        target_tabs=target_tabs, mena_enabled=mena_enabled,
    )

    _print_summary(grouped, other_log, review_rows, total, mena_log, target_tabs=target_tabs)
    if dry_run:
        print("\nDry-run: skipped sheet writes -- no tabs created or modified.", flush=True)
    else:
        if (green_coords or red_coords) and form_responses_sheet_id is not None:
            try:
                color_cells_batch(sheets_service, config.sheet_id, form_responses_sheet_id, green_coords, red_coords)
            except Exception as exc:
                print(f"WARNING: batch cell coloring failed ({len(green_coords)} green, {len(red_coords)} red cells): {exc}", flush=True)
        # Country buckets are colored green (finalized); Human Review is not.
        review_tab = "Human Review"
        mena_tab = "MENA"
        other_tab = "Other Countries"
        effective_tabs = target_tabs if target_tabs is not None else TARGET_TABS
        tab_writes = [
            (title, grouped.get(bucket, []))
            for bucket, title in effective_tabs.items()
        ]
        tab_writes.append((review_tab, review_rows))
        # MENA countries (Qatar, UAE, Oman, Egypt, Algeria, Jordan, Pakistan)
        # get their own visible tab. mena_log rows carry the full source row
        # (all 16 columns) -- same format as every other tab.
        if mena_enabled:
            tab_writes.append((mena_tab, mena_log))
        # Non-target, non-MENA countries (Canada, China, Japan, etc.) get
        # their own visible tab. other_log rows carry the full source row plus
        # a trailing bucket cell; strip the bucket for write_tab_data, which
        # expects the same full-row format as every other tab.
        tab_writes.append((other_tab, [r[:-1] for r in other_log]))
        # Tab cleanup: delete stale country tabs from a prior run that are
        # NOT in this run's tab list (plus the always-written Total
        # Statistics tab). Non-algorithm tabs (CRM, Form Responses 1, etc.) are
        # never deleted. Done before create_sheet_tab so the write loop
        # only recreates tabs this run actually writes.
        new_tab_titles = [title for title, _ in tab_writes] + ["Total Statistics"]
        try:
            _cleanup_stale_tabs(sheets_service, config.sheet_id, new_tab_titles)
        except Exception as exc:
            print(f"WARNING: tab cleanup failed: {type(exc).__name__}: {exc}", flush=True)
        for title, rows in tab_writes:
            create_sheet_tab(sheets_service, config.sheet_id, title)
            write_tab_data(sheets_service, config.sheet_id, title, rows, header=header)
        print(f"\nWrote {len(tab_writes)} tabs into sheet {config.sheet_id}", flush=True)
        # Total Statistics: compute from in-memory tab_writes (not by reading
        # the sheet back, which can return stale data due to API eventual
        # consistency immediately after writes).
        try:
            stats_counts = [(title, len(rows)) for title, rows in tab_writes]
            stats_counts.sort(key=lambda x: x[1], reverse=True)
            total_count = sum(n for _, n in stats_counts)
            stats_rows = [[country, n] for country, n in stats_counts]
            stats_rows.append(["Total", total_count])
            create_sheet_tab(sheets_service, config.sheet_id, "Total Statistics")
            write_tab_data(
                sheets_service, config.sheet_id, "Total Statistics",
                stats_rows, header=["Country", "Count"],
            )
            print(
                f"\nWrote 'Total Statistics' tab ({len(stats_counts)} countries, "
                f"{total_count} total startups).",
                flush=True,
            )
        except Exception as exc:
            print(f"WARNING: Total Statistics write failed: {type(exc).__name__}: {exc}", flush=True)
    return {
        "classified": total - len(errors),
        "errors": errors,
        "excluded": len(other_log),
        "mena": len(mena_log),
        "review": len(review_rows),
        "tabs": {b: len(grouped.get(b, [])) for b in TARGET_TABS},
    }
