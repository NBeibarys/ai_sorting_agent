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

# Tabs that must NEVER be deleted by tab cleanup, even if they are not in
# the current run's tab list. These hold source data or cross-run state.
PROTECTED_TABS = {
    "Form Responses 1",
    "Total Statistics",
    "CRM",
}

COL_NEEDLES = {}  # name/country/founder/etc are added dynamically in _find_columns
# Display-only reference columns: a missing needle yields "" instead of
# crashing the whole batch.
OPTIONAL_NEEDLES = {"founder", "email", "telegram", "pitch_deck", "dedup"}

# Output tab header: all 16 source columns written verbatim into every
# output tab, one row per startup. Matches the alchemist sheet column order.
OUTPUT_HEADER = [
    "Timestamp",
    "Email Address",
    "Score",
    "Startup name",
    "Which country do most of your team members come from?",
    "Startup website",
    "What specific problem does your company solve for B2B customers?",
    "What was your revenue in 2025 and 2026 (in USD)?",
    "Please share your pitch deck presentation link",
    "Please insert link to a <3 min video pitching in English",
    "What is the full name of your CEO?",
    "What is your CEO's email?",
    "What is your CEO's Telegram account (or Whatsapp number)?",
    "Is your startup entity registered in Delaware (U.S.)?",
    "Do you have a VISA to travel to the United States?",
    "Where is your startup incorporated?",
]



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
    email_col_idx=None,
    name_col_idx=None,
) -> list:
    """Drop duplicate rows by DEDUP_COLUMN value, then by email, then fuzzy name.

    Three passes (in order), each only dropping a row if it matches an
    already-kept row from an EARLIER pass:

    1. EXACT match on the raw dedup cell (case-insensitive, whitespace
       stripped) -- same as the original behavior.
    2. EMAIL match: if two kept rows share the same non-empty email, the
       later one is dropped.
    3. FUZZY name match: normalize names (strip punctuation, collapse
       whitespace, lowercase, accent-fold) and drop later rows whose
       normalized name matches an earlier kept row.

    Empty/null values are NEVER a duplicate (every empty row is kept).
    Only the first occurrence of each value is kept; later duplicates are
    dropped entirely and excluded from classification and output. Each
    removed duplicate is logged with the reason ('exact', 'email', 'fuzzy').

    `row_meta[i]` carries the original source row index in tuple position 0;
    that index is preserved so source-cell coloring still maps to the right
    row. `rows` is the raw source rows list so we can read cells directly
    via their column index.

    `email_col_idx` and `name_col_idx` are optional column indices used by
    the email and fuzzy passes. When None (no such column), those passes are
    skipped and only exact dedup runs.
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
    removed = {"exact": 0, "email": 0, "fuzzy": 0}
    kept: list = []  # list of meta_entry
    seen_exact: set = set()
    seen_emails: set = set()
    seen_fuzzy_names: set = set()

    for meta_entry in row_meta:
        i = meta_entry[0]
        raw = _cell_val(dedup_col_idx, i)
        exact_key = raw.lower()
        if not exact_key:
            # Empty dedup value -> never an exact duplicate; keep this row
            # but still let email/fuzzy passes compare it.
            exact_key = None
        # PASS 1: exact dedup
        if exact_key is not None and exact_key in seen_exact:
            removed["exact"] += 1
            print(
                f"  dedup-exact: row {i} dropped (duplicate of earlier row "
                f"on '{column_name}': {raw!r})",
                flush=True,
            )
            continue
        if exact_key is not None:
            seen_exact.add(exact_key)
        # PASS 2: email dedup
        email = _cell_val(email_col_idx, i) if email_col_idx is not None else ""
        if email:
            email_key = email.lower()
            if email_key in seen_emails:
                removed["email"] += 1
                print(
                    f"  dedup-email: row {i} dropped (same email {email!r} "
                    f"as an earlier row)",
                    flush=True,
                )
                continue
            seen_emails.add(email_key)
        # PASS 3: fuzzy name dedup (name column is the dedup column)
        name_col = name_col_idx if name_col_idx is not None else dedup_col_idx
        name_val = _cell_val(name_col, i)
        fuzzy_key = _normalize_for_fuzzy(name_val)
        if fuzzy_key:
            if fuzzy_key in seen_fuzzy_names:
                removed["fuzzy"] += 1
                print(
                    f"  dedup-fuzzy: row {i} dropped (normalized name "
                    f"{fuzzy_key!r} matches an earlier row)",
                    flush=True,
                )
                continue
            seen_fuzzy_names.add(fuzzy_key)
        kept.append(meta_entry)

    total_removed = sum(removed.values())
    print(
        f"Found {total_removed} duplicates in column '{column_name}' "
        f"(exact={removed['exact']}, email={removed['email']}, "
        f"fuzzy={removed['fuzzy']}), removing...",
        flush=True,
    )
    print(
        f"Deduplication: removed {total_removed} of {total} rows "
        f"({len(kept)} unique remaining).",
        flush=True,
    )
    return kept


def _cleanup_stale_tabs(sheets_service, sheet_id: str, new_tab_titles: list) -> list:
    """Delete tabs that exist in the sheet but are NOT in this run's tab list.

    Prevents stale country tabs from a prior run lingering with old data.
    Tabs in PROTECTED_TABS ('Form Responses 1', 'Total Statistics', 'CRM')
    are never deleted. Returns the list of deleted tab titles (for logging).

    Idempotent and safe: each delete is a separate batchUpdate, a transient
    failure on one tab does not block the others, and the function logs
    every deletion so an operator can see what was removed.
    """
    existing = list_existing_tab_titles(sheets_service, sheet_id)
    new_set = set(new_tab_titles)
    to_delete = [
        title for title in existing
        if title not in new_set and title not in PROTECTED_TABS
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
        email_col_idx=cols.get("email"),
        name_col_idx=cols.get("name"),
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
        # Statistics tab). PROTECTED_TABS ('Form Responses 1', 'CRM') are
        # never deleted. Done before create_sheet_tab so the write loop
        # only recreates tabs this run actually writes.
        new_tab_titles = [title for title, _ in tab_writes] + ["Total Statistics"]
        try:
            _cleanup_stale_tabs(sheets_service, config.sheet_id, new_tab_titles)
        except Exception as exc:
            print(f"WARNING: tab cleanup failed: {type(exc).__name__}: {exc}", flush=True)
        for title, rows in tab_writes:
            create_sheet_tab(sheets_service, config.sheet_id, title)
            write_tab_data(sheets_service, config.sheet_id, title, rows, header=OUTPUT_HEADER)
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
