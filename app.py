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

# Substring needles for auto-detecting column mappings from sheet headers.
# These columns are NOT user-selectable — the sidebar only exposes Sheet,
# Tab, Classify column, Dedup column, Output columns, and Countries.
_COL_NEEDLES = {
    "name": ["startup", "name"],
    "founder": ["ceo", "founder", "your name"],
    "email": ["email"],
    "telegram": ["telegram"],
    "pitch_deck": ["pitch", "deck"],
}


def _default_index(headers: list[str], needles: list[str]) -> int:
    """Index of the first header (lowercased) containing any needle, else 0."""
    lowered = [h.lower() for h in headers]
    for needle in needles:
        for i, low in enumerate(lowered):
            if needle in low:
                return i
    return 0


def _auto_detect_column(
    headers: list[str], needles: list[str], exclude: list[str] | None = None
) -> str:
    """Find the first header containing any needle (case-insensitive).

    A header containing any exclude substring is skipped. Falls back to
    headers[0] when nothing matches (or "" if headers is empty).
    """
    exclude = exclude or []
    lowered = [h.lower() for h in headers]
    for needle in needles:
        for i, low in enumerate(lowered):
            if needle in low and not any(ex in low for ex in exclude):
                return headers[i]
    return headers[0] if headers else ""


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
    if grand_total == 0 and not df.empty:
        grand_total = int(df["Count"].sum())

    if not df.empty and grand_total:
        df["Percentage"] = (df["Count"] / grand_total * 100).round(1)
    else:
        df["Percentage"] = pd.Series(dtype=float)

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
    """Run run_batch with the user-selected countries passed as parameters.

    - Selected target countries: kept in target_tabs -> dedicated tab.
    - Deselected target countries: fall through to Other Countries.
    - MENA selected: mena_enabled=True -> rows go to MENA tab.
    - MENA deselected: mena_enabled=False -> MENA rows go to Other.
    """
    selected_set = set(selected_countries)
    filtered_tabs = OrderedDict(
        (k, v) for k, v in pipeline_mod.TARGET_TABS.items()
        if k in selected_set
    )
    mena_selected = "MENA" in selected_set
    return run_batch(
        config, dry_run=False, force=False,
        target_tabs=filtered_tabs, mena_enabled=mena_selected,
    )


class _StreamlitStream:
    """A file-like stdout replacement that streams pipeline output to a
    Streamlit st.status() container in real-time.

    Pipeline functions (run_batch, _print_summary, etc.) print progress
    messages via print(..., flush=True). This class intercepts those
    writes and renders each completed line via st.text() so the user
    sees progress as it happens, not after run_batch() returns.
    """

    def __init__(self):
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                st.text(line)
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            st.text(self._buf)
        self._buf = ""

    def isatty(self) -> bool:
        return False


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

    # Email column — user-selectable for email-based dedup. Defaults to the
    # header containing "email" (e.g. "Email Address" or "CEO's email"). A
    # "(none)" option lets the user skip email-based dedup entirely; in that
    # case pipeline.py drops the email pass and runs exact + fuzzy only.
    _NONE = "(none)"
    email_options = [_NONE] + headers
    default_email_idx = _default_index(headers, _COL_NEEDLES["email"]) + 1
    email_col = st.sidebar.selectbox(
        "Email Column",
        email_options,
        index=default_email_idx,
        help=(
            "Used ONLY for the email-based dedup pass. Pick '(none)' to "
            "skip email dedup and run exact + fuzzy name dedup only."
        ),
    )
    if email_col == _NONE:
        email_col = ""  # empty -> pipeline skips email dedup gracefully

    # ── Auto-detected column mappings (not user-selectable) ────────────
    # founder / telegram / pitch-deck / startup-name are all auto-detected
    # from the sheet headers via substring matching, so the app works for
    # Alchemist, R2B, or any future sheet without code changes. Email and
    # the dedup column are user-selectable (above) since they drive the
    # dedup passes directly.
    name_col = _auto_detect_column(
        headers, _COL_NEEDLES["name"], exclude=["ceo", "founder"]
    )
    founder_col = _auto_detect_column(headers, _COL_NEEDLES["founder"])
    telegram_col = _auto_detect_column(headers, _COL_NEEDLES["telegram"])
    pitch_deck_col = _auto_detect_column(headers, _COL_NEEDLES["pitch_deck"])

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

        # BEFORE: compute counts so the user knows what's about to happen.
        cp_before = len(_load_checkpoint(config.checkpoint_path))
        try:
            _, src_rows = _read_tab(sheet_id, tab)
            total_rows = len(src_rows)
        except Exception:
            total_rows = 0

        new_to_classify = max(total_rows - cp_before, 0)

        # If the checkpoint already covers all source rows, there's nothing
        # to do — tell the user immediately without running the pipeline.
        if new_to_classify == 0 and total_rows > 0:
            st.info(
                f"No new rows to classify. All {total_rows} rows are "
                f"already in the checkpoint."
            )
        else:
            with st.status("Classifying new rows...", expanded=True) as status:
                if total_rows > 0:
                    st.write(
                        f"Total rows: {total_rows} | Already classified: "
                        f"{cp_before} | New rows to classify: "
                        f"{new_to_classify}"
                    )

                error_msg = None
                result = None
                stream = _StreamlitStream()
                old_stdout = sys.stdout
                sys.stdout = stream
                try:
                    result = _run_classify_with_selections(
                        config, selected_countries
                    )
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc}"
                finally:
                    sys.stdout = old_stdout
                    stream.flush()

                if error_msg:
                    st.error(f"Classification failed: {error_msg}")
                    status.update(
                        label="Classification failed", state="error"
                    )
                else:
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

    st.metric("Total Startups", grand_total)

    if not stats_df.empty:
        stats_df = stats_df.sort_values("Count", ascending=False).reset_index(drop=True)
        col_chart, col_pie = st.columns(2)
        with col_chart:
            bar_df = stats_df.copy()
            fig_bar = px.bar(
                bar_df, x="Count", y="Country",
                title="Country Distribution",
                orientation="h",
            )
            fig_bar.update_yaxes(tickangle=0)
            fig_bar.update_yaxes(categoryorder="total ascending")
            st.plotly_chart(fig_bar, use_container_width=True)
        with col_pie:
            pie_df = stats_df.copy()
            fig_pie = px.pie(
                pie_df, values="Count", names="Country",
                title="Country Percentage",
            )
            fig_pie.update_traces(
                textposition="inside",
                textinfo="percent+label",
            )
            # Hide labels on 0-count slices to avoid clutter
            fig_pie.update_traces(
                text=stats_df.apply(
                    lambda r: f"{r['Country']} {r['Percentage']}%" if r["Count"] > 0 else "",
                    axis=1,
                )
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        st.dataframe(stats_df, use_container_width=True, hide_index=True)
    else:
        st.info("No statistics available yet. Click 'Classify New Rows' to generate.")

    st.divider()

    # ── Startup Table ──────────────────────────────────────────────────
    st.header("Startup Table")

    # Dropdown of available country tabs (not column values)
    available_tabs = []
    svc = None
    try:
        svc = _get_sheets_service()
    except Exception as exc:
        st.error(f"Could not connect to Google Sheets: {exc}")

    if svc:
        # Get all sheet tab names in ONE API call (batchGet) instead of
        # checking each tab individually (avoids 60 read/min rate limit).
        try:
            spreadsheet = svc.spreadsheets().get(
                spreadsheetId=sheet_id, fields="sheets(properties.title,properties.gridProperties)"
            ).execute()
            existing_tab_titles = {
                s["properties"]["title"]
                for s in spreadsheet.get("sheets", [])
            }
            # Only show country tabs that exist in the sheet
            available_tabs = [
                t for t in COUNTRY_TABS if t in existing_tab_titles
            ]
        except Exception:
            available_tabs = list(COUNTRY_TABS)  # fallback: show all

    if available_tabs:
        selected_tab = st.selectbox(
            "Filter by Country", ["All Countries"] + available_tabs
        )

        # Read data: single tab if selected, all tabs if "All Countries"
        header, rows = [], []
        if selected_tab == "All Countries":
            try:
                header, rows = _read_all_country_tabs(sheet_id)
            except Exception as exc:
                st.error(f"Could not read country tabs: {exc}")
        else:
            try:
                header, rows = read_sheet_rows(svc, sheet_id, selected_tab)
            except Exception as exc:
                st.error(f"Could not read {selected_tab} tab: {exc}")

    if header and rows:
        df = pd.DataFrame(rows, columns=header)

        # Show ONLY 2 columns:
        # Column 1: ALWAYS "Startup name" (hardcoded - not user-selectable)
        # Column 2: classify_col (Classification column selected by user in sidebar)
        keep: list[str] = []
        if "Startup name" in df.columns:
            keep.append("Startup name")
        if classify_col in df.columns and classify_col != "Startup name":
            keep.append(classify_col)

        df = df[keep] if keep else df

        st.dataframe(df, use_container_width=True, hide_index=True)
        # NOTE: CSV download intentionally removed — corporate security
        # requirement: data must stay in the app, no exports.
    elif not available_tabs:
        st.info("No data to display. Run classification first.")
    else:
        st.info(f"No data in selected tab. Try another country or run classification.")


if __name__ == "__main__":
    main()
