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
    """Write [Startup Name, Timestamp, Incorporated, HQ Country] rows to a tab.

    Each row in `rows` is a 4-tuple (name, timestamp, incorporated_raw,
    country_raw) copied verbatim from the source form responses -- these are
    reference columns for display only and never feed the classifier. Clears
    the tab first so a re-run with fewer matches in a bucket does not leave
    the previous run's longer list visible below the new data. Always writes
    a header row, even when `rows` is empty, so every tab has a consistent
    shape.
    """
    values = [[
        "Startup Name",
        "Timestamp",
        "Where is your startup incorporated?",
        "In which country is your startup physically headquartered?",
    ]]
    values.extend(
        [name, timestamp, incorporated, country]
        for name, timestamp, incorporated, country in rows
    )
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
    # Color the startup-name column (A) emerald green for all data rows.
    # values().update() only writes cell data — the Sheets API applies
    # cell formatting via a separate batchUpdate repeatCell request.
    # Tabs are addressed by their integer sheetId (not title), so resolve
    # this tab's sheetId by matching its title against the spreadsheet's
    # sheet properties (same lookup create_sheet_tab performs).
    num_data_rows = len(values) - 1
    if num_data_rows > 0:
        meta = (
            sheets_service.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets/properties")
            .execute()
        )
        tab_sheet_id = None
        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("title") == title:
                tab_sheet_id = sheet.get("properties", {}).get("sheetId")
                break
        if tab_sheet_id is not None:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": tab_sheet_id,
                                    "startRowIndex": 1,
                                    "endRowIndex": num_data_rows + 1,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": 1,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": {
                                            "red": 0.0,
                                            "green": 0.804,
                                            "blue": 0.4,
                                        }
                                    }
                                },
                                "fields": "userEnteredFormat.backgroundColor",
                            }
                        }
                    ]
                },
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


def color_name_cell(sheets_service, spreadsheet_id, sheet_id_int, row_index, col_index):
    """Color a single cell emerald green via batchUpdate repeatCell.

    Marks processed rows in the source "Form Responses 1" tab: once a row is
    classified, its startup-name cell gets a green background so an operator
    can see at a glance which rows the pipeline has touched. Indices are
    0-indexed (Sheets API GridRange convention) -- sheet row 2 / column B is
    row_index=1, col_index=1.
    """
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
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
                                "backgroundColor": {
                                    "red": 0.0,
                                    "green": 0.804,
                                    "blue": 0.4,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            ]
        },
    ).execute()
