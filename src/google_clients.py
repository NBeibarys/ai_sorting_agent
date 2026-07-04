"""
Google Sheets access via a single service account.
Service account (not OAuth) chosen because a batch run must be
non-interactive — no browser consent prompt mid-run. The account's
email needs Editor access on the Sheet (required to create tabs and
write results back into the same spreadsheet).
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def _credentials(service_account_path: str):
    return service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )


def get_sheets_service(service_account_path: str):
    """Build an authenticated Sheets v4 service from the service account."""
    creds = _credentials(service_account_path)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet_rows(sheets_service, sheet_id: str, sheet_range: str, header_row: int = 1):
    """Return (header, rows). Row N in `rows` corresponds to sheet row
    N + header_row + 1 (sheet rows are 1-indexed).

    header_row defaults to 1 (the common case: first row is the header).
    """
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=sheet_range)
        .execute()
    )
    values = result.get("values", [])
    if len(values) < header_row:
        return [], []
    header, rows = values[header_row - 1], values[header_row:]
    # Sheets API drops trailing empty cells per row — pad so every row
    # lines up positionally with `header`, or later index lookups
    # silently grab the wrong column for short rows.
    width = len(header)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return header, rows


def _col_letter(col_1_indexed: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA. Sheets API ranges use letters, not indices."""
    letters = ""
    n = col_1_indexed
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def create_sheet_tab(sheets_service, sheet_id: str, title: str) -> None:
    """Create a new tab in the spreadsheet if it does not already exist.

    Idempotent: a re-run must not crash on tabs created by a prior run.
    Existing tabs are left in place — write_tab_data clears their content
    before writing, so stale rows from a previous classification never
    survive a re-run.
    """
    meta = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets/properties")
        .execute()
    )
    existing_titles = {
        sheet.get("properties", {}).get("title")
        for sheet in meta.get("sheets", [])
    }
    if title in existing_titles:
        return
    body = {
        "requests": [
            {"addSheet": {"properties": {"title": title}}}
        ]
    }
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body=body
    ).execute()


def write_tab_data(sheets_service, sheet_id: str, title: str, rows, header: list | None = None) -> None:
    """Write rows to a tab.

    Each row in `rows` is a list whose length matches `header`. The full
    source row (all columns) is copied verbatim -- one row per startup, every
    field. These are reference columns for display only and never feed the
    classifier. Clears the tab first so a re-run with fewer matches in a
    bucket does not leave the previous run's longer list visible below the
    new data. Always writes a header row, even when `rows` is empty, so every
    tab has a consistent shape.

    `header` defaults to a legacy 8-column layout for backward compatibility;
    on the alchemist sheet all 16 source columns are passed through. Rows
    shorter than the header are right-padded with empty strings so every
    output row lines up with the header.
    """
    if header is None:
        header = [
            "Startup Name",
            "Founder Name",
            "Email",
            "Telegram Handle",
            "Pitch Deck",
            "Timestamp",
            "Where is your startup incorporated?",
            "In which country is your startup physically headquartered?",
        ]
    width = len(header)
    values = [list(header)]
    for row in rows:
        cells = list(row)
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))
        elif len(cells) > width:
            cells = cells[:width]
        values.append(cells)
    # Clear any prior content across the actual data extent before writing.
    # Uses the header width to compute the last column letter (so a schema
    # wider than Z is covered) and a generous row ceiling.
    last_col_letter = _col_letter(width)
    clear_end_row = max(len(values), 1) + 1000  # pad beyond new data
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"{title}!A1:{last_col_letter}{clear_end_row}",
        body={},
    ).execute()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{title}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def list_existing_tab_titles(sheets_service, spreadsheet_id: str) -> list[str]:
    """Return the titles of every tab currently in the spreadsheet.

    Used by tab cleanup to find stale country tabs left over from a prior run
    that are no longer in the current run's tab list.
    """
    meta = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets/properties")
        .execute()
    )
    return [
        sheet.get("properties", {}).get("title")
        for sheet in meta.get("sheets", [])
        if sheet.get("properties", {}).get("title")
    ]


def delete_sheet_tab(sheets_service, sheet_id: str, title: str) -> None:
    """Delete a tab from the spreadsheet by title.

    Looks up the integer sheetId for the title, then issues a batchUpdate
    with a deleteSheet request. Silently does nothing if the tab does not exist
    (idempotent: a prior cleanup run may have already removed it).
    """
    sheet_id_int = get_sheet_id_by_title(sheets_service, sheet_id, title)
    if sheet_id_int is None:
        return
    body = {
        "requests": [
            {"deleteSheet": {"sheetId": sheet_id_int}}
        ]
    }
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body=body
    ).execute()


def get_sheet_id_by_title(sheets_service, spreadsheet_id, title):
    """Return the integer sheetId for a tab by its title.

    batchUpdate addresses tabs by their integer sheetId, not by title, so
    coloring a cell in the source "Form Responses 1" tab needs this lookup
    first. Returns None if no tab matches the title.
    """
    meta = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets/properties")
        .execute()
    )
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == title:
            return props.get("sheetId")
    return None


def color_cells_batch(
    sheets_service,
    spreadsheet_id,
    sheet_id_int,
    green_coords,
    red_coords,
    *,
    chunk_size=50,
    max_retries=4,
):
    """Color cells emerald green or pleasant red in chunked batchUpdate calls.

    Same per-cell formatting (emerald green / pleasant red), but batches every cell into
    spreadsheets().batchUpdate() calls so coloring N rows costs ceil(N/50)
    API calls instead of N. The Sheets write quota is 60/min; the per-row
    variant burned through it on a ~687-row run and 429'd the downstream tab
    writes.

    A single batchUpdate with all 687 requests at once exceeds Google's per-
    request payload/SSL limits and fails with http2/SSL EOF errors on large
    datasets. The requests are therefore split into chunks of `chunk_size`
    (default 50) repeatCell requests, each sent as its own batchUpdate call.
    Transient transport errors (ssl.SSLError, ConnectionError, TimeoutError,
    googleapiclient HttpError 5xx) are retried with fixed backoff (5s/10s/20s,
    clamped on the 4th attempt), aligned with the chunk retry in workflow.py.

    green_coords: list of (row_index, col_index) 0-indexed tuples colored
    emerald green (red=0.0, green=0.804, blue=0.4) -- normal classifications.
    red_coords: list of (row_index, col_index) 0-indexed tuples colored
    pleasant red (red=0.9, green=0.2, blue=0.2) -- Human Review rows.
    """
    import ssl
    import time

    from googleapiclient.errors import HttpError

    if not green_coords and not red_coords:
        return

    def _repeat_cell(row_index, col_index, rgb):
        return {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id_int,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": rgb
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        }

    GREEN = {"red": 0.0, "green": 0.804, "blue": 0.4}
    RED = {"red": 0.9, "green": 0.2, "blue": 0.2}
    requests = (
        [_repeat_cell(r, c, GREEN) for r, c in green_coords]
        + [_repeat_cell(r, c, RED) for r, c in red_coords]
    )

    def _is_transient(exc):
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            return True
        if isinstance(exc, HttpError):
            status = getattr(exc, "resp", None)
            status = getattr(status, "status", None) if status else None
            try:
                return int(status) >= 500 if status else False
            except (TypeError, ValueError):
                return False
        # googleapiclient may surface transport errors as generic Exception
        # wrapping an SSLError; treat EOF/transport markers as transient.
        msg = str(exc).lower()
        return any(
            m in msg
            for m in ("ssl", "eof", "connection reset", "broken pipe", "timed out")
        )

    # Fixed backoff aligned with workflow.py chunk retry (5s/10s/20s).
    # The Sheets write quota is 60/min; a 429 means the quota is exhausted
    # and retrying after 1-8s (the old exponential values) hits the same
    # exhausted quota. 5s/10s/20s gives the quota window time to recover.
    backoff_seconds = (5, 10, 20)

    total_chunks = (len(requests) + chunk_size - 1) // chunk_size
    for ci in range(0, len(requests), chunk_size):
        chunk = requests[ci : ci + chunk_size]
        chunk_num = ci // chunk_size + 1
        for attempt in range(1, max_retries + 1):
            try:
                sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": chunk},
                ).execute()
                break
            except Exception as exc:
                if attempt == max_retries or not _is_transient(exc):
                    raise
                wait = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                print(
                    f"WARNING: batchUpdate chunk {chunk_num}/{total_chunks} "
                    f"failed (attempt {attempt}/{max_retries}): "
                    f"{type(exc).__name__}: {exc} -- retrying in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
