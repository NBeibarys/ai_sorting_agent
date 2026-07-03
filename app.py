"""
Streamlit dashboard for the AI sorting agent.

Run from the repo root:
    streamlit run app.py

Sidebar controls let the user pick a sheet, tab, classify column, dedup
column, and output columns. The main area shows Total Statistics (summary
table, bar chart, pie chart), a startup table with country filter + CSV
download, and a "Classify New Rows" button that runs the existing pipeline
with a per-sheet checkpoint file.
"""

import io
import os
import sys

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
from src.google_clients import get_sheets_service, read_sheet_rows
from src.pipeline import run_batch, _load_checkpoint, TARGET_TABS

# Sheet IDs stored internally — not in .env, not visible to the user.
SHEETS = {
    "Alchemist": "1eTstP1hQyA9p0_hI17rO42_P16If_5F7jfjJqnnWkXM",
    "R2B": "1nPKrGpVrRsYus7jSPOflRct5Git4-THXsLhsCuXtcVg",
}

# Country tabs for the startup-table filter dropdown.
COUNTRY_TABS = list(TARGET_TABS.keys()) + ["Human Review", "MENA", "Other Countries"]


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


def _build_config(sheet_id, tab, classify_col, dedup_col, sheet_name) -> Config:
    """Build a Config manually from user selections (not from_env).

    Per-sheet checkpoint: checkpoint_alchemist.json / checkpoint_r2b.json
    so the two sheets never mix.
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
        name_column="startup name",
        founder_name_column="full name of your ceo",
        email_column="ceo's email",
        telegram_column="telegram account",
        pitch_deck_column="pitch deck",
        dedup_column=dedup_col,
    )


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
    default_dedup_idx = 0
    for i, h in enumerate(headers):
        if "startup name" in h.lower():
            default_dedup_idx = i
            break
    dedup_col = st.sidebar.selectbox("Dedup Column", headers, index=default_dedup_idx)

    output_cols = st.sidebar.multiselect(
        "Output Columns", headers, default=headers
    )

    if st.sidebar.button("Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    # ── Classify New Rows button ───────────────────────────────────────
    if st.button("Classify New Rows", type="primary"):
        config = _build_config(sheet_id, tab, classify_col, dedup_col, sheet_name)
        cp_before = len(_load_checkpoint(config.checkpoint_path))

        error_msg = None
        result = None
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured

        with st.status("Classifying new rows...", expanded=True) as status:
            try:
                result = run_batch(config, dry_run=False, force=False)
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

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV",
            csv,
            file_name=f"{country_filter.lower().replace(' ', '_')}_startups.csv",
            mime="text/csv",
        )
    else:
        st.info("No data to display. Run classification first.")


if __name__ == "__main__":
    main()
