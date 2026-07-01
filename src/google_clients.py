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


def write_tab_data(sheets_service, sheet_id: str, title: str, rows) -> None:
    """Write [Startup Name, Timestamp] rows to a tab.

    Clears the tab first so a re-run with fewer matches in a bucket does
    not leave the previous run's longer list visible below the new data.
    Always writes a header row, even when `rows` is empty, so every tab
    has a consistent shape.
    """
    values = [["Startup Name", "Timestamp"]]
    values.extend([name, timestamp] for name, timestamp in rows)
    # Clear any prior content across a generous range before writing.
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"{title}!A1:Z100000",
        body={},
    ).execute()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{title}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
