"""
Streamlit dashboard for the AI sorting agent.

Run from the repo root:
    streamlit run app.py

Sidebar controls let the user pick a sheet, tab, classify column, dedup
column, output columns, and which country tabs to generate. The main area
shows Trends, Total Statistics (summary table, bar chart, pie chart), and
a startup table with country filter.

Classification is driven by a per-sheet checkpoint file. The pipeline's
country routing (src/pipeline.TARGET_TABS and _is_mena) is patched at
runtime so only the user-selected countries get dedicated output tabs;
unselected countries fall through to "Other Countries". No data leaves
the app via download -- data stays in the sheet.
"""

import io
import os
import sys
from collections import OrderedDict

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

# ── Path + env setup ───────────────────────────────────────────────────
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_REPO_DIR, ".env"))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from src.config import Config
from src import pipeline as pipeline_mod
from src.google_clients import get_sheets_service, read_sheet_rows
from src.pipeline import run_batch, _load_checkpoint, TARGET_TABS

# Sheet IDs stored internally — not in .env, not visible to the user.
SHEETS = {
    "Alchemist": "1eTstP1hQyA9p0_hI17rO42_P16If_5F7jfjJqnnWkXM",
    "R2B": "1nPKrGpVrRsYus7jSPOflRct5Git4-THXsLhsCuXtcVg",
}

# The 9 countries a user can toggle on/off in the sidebar. "Human Review"
# and "Other Countries" are always generated (catch-alls) and are NOT in
# this list. MENA is selectable: when deselected, MENA-region rows fall
# through to "Other Countries" instead of the dedicated MENA tab.
SELECTABLE_COUNTRIES = [
    "Uzbekistan",
    "Turkiye",
    "Georgia",
    "Kyrgyzstan",
    "Azerbaijan",
    "USA",
    "Kazakhstan",
    "Mong. Turkmenistan Tajikistan",
    "MENA",
]

# Country tabs for the startup-table filter dropdown: every tab that can
# exist in the sheet after a run. Stays the full set so the filter works
# regardless of which countries were selected for the last run.
COUNTRY_TABS = list(TARGET_TABS.keys()) + ["Human Review", "MENA", "Other Countries"]

# Default column auto-detect needles: the first header (case-insensitive)
# containing any of these substrings becomes the default selection.
_COL_NEEDLES = {
    "name": ["startup name", "startup", "company name", "name"],
    "founder": ["full name of your ceo", "ceo name", "founder", "full name"],
    "email": ["ceo's email", "ceo email", "email"],
    "telegram": ["telegram account", "telegram", "whatsapp"],
    "pitch_deck": ["pitch deck", "pitch", "deck"],
}


def _default_index(headers: list[str], needles: list[str]) -> int:
    """Index of the first header (lowercased) containing any needle, else 0."""
    lowered = [h.lower() for h in headers]
    for needle in needles:
        for i, low in enumerate(lowered):
            if needle in low:
                return i
    return 0


# ── Helpers ─────────────────────────────────────────────────────────────

def _resolve_creds() -> str:
    """Resolve GOOGLE_APPLICATION_CREDENTIALS to an absolute path.

    The .env file typically has a relative path (service_account.json).
    Resolve it against the repo root so it works regardless of CWD.
    """
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds and not os.path.isabs(creds):
        creds = os.path.join(_REPO_DIR, creds)
    return creds


@st.cache_resource
def _get_sheets_service():
    """Build an authenticated Sheets v4 service (cached for app lifetime)."""
    creds = _resolve_creds()
    if not creds or not os.path.isfile(creds):
        raise FileNotFoundError(
            f"GOOGLE_APPLICATION_CREDENTIALS not set or file not found: {creds}"
        )
    return get_sheets_service(creds)


@st.cache_data(ttl=120)
def _get_tabs(sheet_id: str) -> list[str]:
    """List all tab titles in the spreadsheet (live read)."""
    svc = _get_sheets_service()
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets/properties")
        .execute()
    )
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


@st.cache_data(ttl=120)
def _get_headers(sheet_id: str, tab: str) -> list[str]:
    """Read the header row from a tab (live read, auto-fills dropdowns)."""
    svc = _get_sheets_service()
    header, _ = read_sheet_rows(svc, sheet_id, tab)
    return header


@st.cache_data(ttl=30)
def _read_tab(sheet_id: str, tab: str) -> tuple[list, list]:
    """Read a tab -> (header, rows). Cached 30s to avoid hammering the API."""
    svc = _get_sheets_service()
    return read_sheet_rows(svc, sheet_id, tab)


@st.cache_data(ttl=30)
def _read_total_stats(sheet_id: str) -> tuple[pd.DataFrame, int]:
    """Read the 'Total Statistics' tab.

    Returns (DataFrame[Country, Count, Percentage], grand_total).
    Returns (empty DataFrame, 0) if the tab doesn't exist yet.
    """
    try:
        svc = _get_sheets_service()
        header, rows = read_sheet_rows(svc, sheet_id, "Total Statistics")
    except Exception:
        return pd.DataFrame(columns=["Country", "Count", "Percentage"]), 0

    data = []
    grand_total = 0
    for row in rows:
        if not row or not row[0].strip():
            continue
        country = row[0].strip()
        try:
            count = int(float(row[1])) if len(row) > 1 and row[1] else 0
        except (ValueError, TypeError):
            count = 0
        if country.lower() == "total":
            grand_total = count
            continue
        data.append({"Country": country, "Count": count})

    df = pd.DataFrame(data)
    if not df.empty:
        total = df["Count"].sum()
        df["Percentage"] = (df["Count"] / total * 100).round(1) if total else 0.0
    else:
        df["Percentage"] = pd.Series(dtype=float)

    if grand_total == 0 and not df.empty:
        grand_total = int(df["Count"].sum())

    return df, grand_total


@st.cache_data(ttl=30)
def _read_all_country_tabs(sheet_id: str) -> tuple[list, list]:
    """Read and concatenate all country tabs -> (header, combined_rows)."""
    svc = _get_sheets_service()
    header = None
    all_rows = []
    for country in COUNTRY_TABS:
        try:
            h, r = read_sheet_rows(svc, sheet_id, country)
            if header is None and h:
                header = h
            all_rows.extend(r)
        except Exception:
            pass
    return header or [], all_rows


def _build_config(
    sheet_id, tab, classify_col, dedup_col, sheet_name,
    name_col, founder_col, email_col, telegram_col, pitch_deck_col,
) -> Config:
    """Build a Config manually from user selections (not from_env).

    Per-sheet checkpoint: checkpoint_alchemist.json / checkpoint_r2b.json
    so the two sheets never mix. Column names come from the sidebar
    dropdowns so the same app works for Alchemist, R2B, and any future
    sheet without code changes.
    """
    return Config(
        sheet_id=sheet_id,
        sheet_range=tab,
        service_account_path=_resolve_creds(),
        use_vertex=True,
        google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        google_cloud_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        model=os.environ.get("SORTER_MODEL", "gemini-3.5-flash"),
        max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "8")),
        checkpoint_path=f"checkpoint_{sheet_name.lower()}.json",
        country_column=classify_col,
        name_column=name_col,
        founder_name_column=founder_col,
        email_column=email_col,
        telegram_column=telegram_col,
        pitch_deck_column=pitch_deck_col,
        dedup_column=dedup_col,
    )


def _run_classify_with_selections(config: Config, selected_countries: list[str]):
    """Run run_batch with the pipeline's country routing patched so only
    the user-selected countries get dedicated output tabs.

    Patches pipeline_mod.TARGET_TABS (used by _route_rows, _print_summary,
    write_total_statistics, run_batch) and pipeline_mod._is_mena (used by
    _route_rows). Restored in a finally block so a Streamlit rerun of the
    script does not see stale state.

    - Selected target countries: kept in TARGET_TABS -> dedicated tab.
    - Deselected target countries: fall through to Other Countries.
    - MENA selected: original _is_mena behavior (rows go to MENA tab).
    - MENA deselected: _is_mena forced False -> MENA rows go to Other.
    """
    selected_set = set(selected_countries)
    filtered_tabs = OrderedDict(
        (k, v) for k, v in pipeline_mod.TARGET_TABS.items()
        if k in selected_set
    )
    mena_selected = "MENA" in selected_set
    orig_tabs = pipeline_mod.TARGET_TABS
    orig_is_mena = pipeline_mod._is_mena
    pipeline_mod.TARGET_TABS = filtered_tabs
    pipeline_mod._is_mena = orig_is_mena if mena_selected else (lambda _raw: False)
    try:
        return run_batch(config, dry_run=False, force=False)
    finally:
        pipeline_mod.TARGET_TABS = orig_tabs
        pipeline_mod._is_mena = orig_is_mena


# ── Main ────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="AI Sorting Dashboard", layout="wide")
    st.title("AI Sorting Dashboard")

    # ── Sidebar controls ────────────────────────────────────────────────
    st.sidebar.header("Controls")

    sheet_name = st.sidebar.selectbox("Sheet", list(SHEETS.keys()))
    sheet_id = SHEETS[sheet_name]

    # Live-read tabs when a sheet is selected.
    try:
        tabs = _get_tabs(sheet_id)
    except Exception as exc:
        st.error(f"Cannot read sheet tabs: {exc}")
        st.stop()

    if not tabs:
        st.error("No tabs found in the selected sheet.")
        st.stop()

    tab = st.sidebar.selectbox("Tab", tabs)

    # Live-read headers from the selected tab.
    try:
        headers = _get_headers(sheet_id, tab)
    except Exception as exc:
        st.error(f"Cannot read headers from tab '{tab}': {exc}")
        st.stop()

    if not headers:
        st.warning("No headers found in the selected tab.")
        st.stop()

    classify_col = st.sidebar.selectbox("Classify Column", headers)

    # Dedup column — default to the startup-name column if present.
    default_dedup_idx = _default_index(headers, _COL_NEEDLES["name"])
    dedup_col = st.sidebar.selectbox("Dedup Column", headers, index=default_dedup_idx)

    # ── Column selectors (fix hardcoded Alchemist column names) ────────
    # Each dropdown auto-fills from the sheet headers and defaults to the
    # first header containing a recognizable needle, so the app works for
    # R2B, Alchemist, or any future sheet without code changes.
    st.sidebar.markdown("---")
    st.sidebar.subheader("Column Mapping")

    name_col = st.sidebar.selectbox(
        "Startup Name Column", headers,
        index=_default_index(headers, _COL_NEEDLES["name"]),
    )
    founder_col = st.sidebar.selectbox(
        "Founder / CEO Name Column", headers,
        index=_default_index(headers, _COL_NEEDLES["founder"]),
    )
    email_col = st.sidebar.selectbox(
        "Email Column", headers,
        index=_default_index(headers, _COL_NEEDLES["email"]),
    )
    telegram_col = st.sidebar.selectbox(
        "Telegram Column", headers,
        index=_default_index(headers, _COL_NEEDLES["telegram"]),
    )
    pitch_deck_col = st.sidebar.selectbox(
        "Pitch Deck Column", headers,
        index=_default_index(headers, _COL_NEEDLES["pitch_deck"]),
    )

    output_cols = st.sidebar.multiselect(
        "Output Columns", headers, default=headers
    )

    # ── Country selector ───────────────────────────────────────────────
    # Multi-select for which country tabs to generate. Default: all 9.
    # "Human Review" and "Other Countries" are always generated (catch-alls)
    # and are not user-selectable.
    st.sidebar.markdown("---")
    st.sidebar.subheader("Country Tabs to Generate")
    selected_countries = st.sidebar.multiselect(
        "Countries",
        SELECTABLE_COUNTRIES,
        default=SELECTABLE_COUNTRIES,
        help=(
            "Only generate tabs for the selected countries. Rows that "
            "don't match any selected country go to 'Other Countries'. "
            "'Human Review' and 'Other Countries' are always generated."
        ),
    )
    if not selected_countries:
        st.sidebar.warning(
            "No countries selected — every classified row will land in "
            "'Other Countries'. Select at least one country."
        )

    if st.sidebar.button("Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    # ── Trends ─────────────────────────────────────────────────────────
    # X total applications in source tab, Y classified (in checkpoint),
    # Z = X - Y new since last run. Live reads, cached 30s.
    st.header("Trends")
    try:
        _src_header, src_rows = _read_tab(sheet_id, tab)
        total_apps = len(src_rows)
    except Exception as exc:
        st.warning(f"Could not read source tab for trends: {exc}")
        total_apps = 0

    checkpoint_path = f"checkpoint_{sheet_name.lower()}.json"
    classified_count = len(_load_checkpoint(checkpoint_path))
    new_since_last = max(total_apps - classified_count, 0)

    t1, t2, t3 = st.columns(3)
    t1.metric("Total Applications", total_apps)
    t2.metric("Classified", classified_count)
    t3.metric("New Since Last Run", new_since_last)
    st.caption(
        f"Source tab: **{tab}** · Checkpoint: `{checkpoint_path}` "
        f"({classified_count} rows classified so far)."
    )

    # ── Classify New Rows button ───────────────────────────────────────
    if st.button("Classify New Rows", type="primary"):
        config = _build_config(
            sheet_id, tab, classify_col, dedup_col, sheet_name,
            name_col, founder_col, email_col, telegram_col, pitch_deck_col,
        )
        cp_before = len(_load_checkpoint(config.checkpoint_path))

        error_msg = None
        result = None
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured

        with st.status("Classifying new rows...", expanded=True) as status:
            try:
                result = _run_classify_with_selections(config, selected_countries)
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
            finally:
                sys.stdout = old_stdout

            if error_msg:
                st.error(f"Classification failed: {error_msg}")
                status.update(label="Classification failed", state="error")
            else:
                # Show captured pipeline output as progress messages.
                for line in captured.getvalue().splitlines():
                    if line.strip():
                        st.text(line)

                cp_after = len(_load_checkpoint(config.checkpoint_path))
                new_rows = cp_after - cp_before

                status.update(
                    label=(
                        f"Done — {result['classified']} classified | "
                        f"{cp_before} already in checkpoint | "
                        f"{new_rows} new"
                    ),
                    state="complete",
                )

        # Auto-refresh the dashboard to show updated data.
        if result is not None:
            st.cache_data.clear()
            st.rerun()

    st.divider()

    # ── Dashboard: Total Statistics ────────────────────────────────────
    st.header("Total Statistics")

    try:
        stats_df, grand_total = _read_total_stats(sheet_id)
    except Exception as exc:
        st.warning(f"Could not read Total Statistics: {exc}")
        stats_df = pd.DataFrame()
        grand_total = 0

    m1, m2 = st.columns(2)
    m1.metric("Total Startups", grand_total)
    m2.metric("Countries", len(stats_df))

    if not stats_df.empty:
        col_chart, col_pie = st.columns(2)
        with col_chart:
            fig_bar = px.bar(
                stats_df, x="Country", y="Count",
                title="Country Distribution",
            )
            fig_bar.update_xaxes(tickangle=45)
            st.plotly_chart(fig_bar, use_container_width=True)
        with col_pie:
            fig_pie = px.pie(
                stats_df, values="Count", names="Country",
                title="Country Percentage",
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        st.dataframe(stats_df, use_container_width=True, hide_index=True)
    else:
        st.info("No statistics available yet. Click 'Classify New Rows' to generate.")

    st.divider()

    # ── Startup Table ──────────────────────────────────────────────────
    st.header("Startup Table")

    country_filter = st.selectbox("Filter by Country", ["All"] + COUNTRY_TABS)
    search = st.text_input("Search", placeholder="Type to filter rows...")

    if country_filter == "All":
        try:
            header, rows = _read_all_country_tabs(sheet_id)
        except Exception as exc:
            st.error(f"Could not read country tabs: {exc}")
            header, rows = [], []
    else:
        try:
            header, rows = _read_tab(sheet_id, country_filter)
        except Exception as exc:
            st.error(f"Could not read tab '{country_filter}': {exc}")
            header, rows = [], []

    if header and rows:
        df = pd.DataFrame(rows, columns=header)

        # Honor output-column selection: keep only selected columns that
        # exist in this tab's headers. If none match, show all columns.
        available = [c for c in output_cols if c in df.columns]
        if available:
            df = df[available]

        # Text search across all visible columns.
        if search:
            mask = df.apply(
                lambda r: search.lower()
                in " ".join(str(v) for v in r).lower(),
                axis=1,
            )
            df = df[mask]

        st.dataframe(df, use_container_width=True, hide_index=True)
        # NOTE: CSV download intentionally removed — corporate security
        # requirement: data must stay in the app, no exports.
    else:
        st.info("No data to display. Run classification first.")


if __name__ == "__main__":
    main()
