#!/usr/bin/env python3
"""
import_statement.py

End-to-end pipeline: bank statement -> parsed CSVs -> "Expenses Override" sheet.

Mirrors the logic of the existing "Expense CSV Importer" Apps Script:
  - Sheet: "Expenses Override", columns: Purchase Date | Item | Amount | Category | Needs Review
    - expenses_<parser>_*.csv rows are written with Needs Review = FALSE
    - review_<parser>_*.csv rows are written with Needs Review = TRUE
  - CSV columns expected: date, amount, category, description (description -> Item)
  - After appending, the whole table is re-sorted by Purchase Date (oldest first)

Usage:
    python import_statement.py /path/to/statement.pdf

Setup required before running:
    1. pip install google-api-python-client google-auth
    2. Create a service account in your GCP project, download its JSON key,
       save it as service_account.json next to this script.
    3. Share your tracker Google Sheet with the service account's email
       (found inside the JSON key as "client_email") as Editor.
    4. Fill in SPREADSHEET_ID below.
"""

import csv
import json
import logging
import sys
from pathlib import Path

import process_statement
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SPREADSHEET_ID = "1JlIH41lNNVPEa3WJa9E7mZP5q3yY5YHm52vdJVK2PnI"
# TODO Change back to "Expenses Override"
SHEET_NAME = "Expenses Override Test"
NUM_COLUMNS = 5  # Purchase Date, Item, Amount, Category, Needs Review

WORK_DIR = Path("./output")

# Tracks which statements have already been imported, so re-running the
# script on the same month is a safe no-op (replaces the Apps Script's
# processed-Drive-file-ID tracking, since we don't need Drive at all here).
STATE_FILE = Path("./processed_statements.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Parse the bank statement -> expenses_YYYY-MM.csv, review_YYYY-MM.csv
# ---------------------------------------------------------------------------
def parse_statement(parser_type: str, statement_path: Path) -> tuple[Path, Path, str]:
    """
    Plug in your existing parser here. It should write:
        WORK_DIR/expenses_YYYY-MM.csv
        WORK_DIR/review_YYYY-MM.csv
    with columns: date, amount, category, description

    Returns (expenses_csv_path, review_csv_path, statement_id) where
    statement_id (e.g. "2026-07") is used for the dedup check below.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    expenses_csv, review_csv, _credits_csv, month_tag = process_statement.main([
        str(statement_path),
        "--rules",
        str(Path(__file__).with_name("category_rules.json")),
        "--out",
        str(WORK_DIR),
    ], parser_type=parser_type)

    statement_id = f"{parser_type}:{month_tag}"

    if not expenses_csv.exists() or not review_csv.exists():
        raise FileNotFoundError(
            "Parser did not produce expected CSVs. Check parse_statement()."
        )
    return expenses_csv, review_csv, statement_id


# ---------------------------------------------------------------------------
# Dedup tracking (replaces the Apps Script's processed-file-ID set)
# ---------------------------------------------------------------------------
def load_processed() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_processed(processed: set[str]):
    STATE_FILE.write_text(json.dumps(sorted(processed)))


# ---------------------------------------------------------------------------
# Step 2: Load a CSV and map to sheet row shape
#          CSV columns: date, amount, category, description
#          Sheet columns: Purchase Date, Item, Amount, Category, Needs Review
# ---------------------------------------------------------------------------
def csv_to_rows(csv_path: Path) -> list[list]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        return []

    header = [h.strip().lower() for h in rows[0]]
    try:
        date_idx = header.index("date")
        amount_idx = header.index("amount")
        desc_idx = header.index("description")
    except ValueError as e:
        raise ValueError(
            "CSV header must include date, amount, description columns."
        ) from e
    category_idx = header.index("category") if "category" in header else None

    out = []
    for r in rows[1:]:
        if len(r) <= 1 or not r[date_idx]:
            continue
        out.append([
            r[date_idx],                                  # Purchase Date
            r[desc_idx],                                  # Item
            float(r[amount_idx]),                         # Amount
            r[category_idx] if category_idx is not None else "",  # Category
        ])
    return out


# ---------------------------------------------------------------------------
# Step 3: Append rows, then re-sort the table by Purchase Date
# ---------------------------------------------------------------------------
def append_rows(sheets_service, rows: list[list]):
    if not rows:
        log.info("No rows to write, skipping.")
        return
    result = (
        sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:E1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        )
        .execute()
    )
    updated = result.get("updates", {}).get("updatedCells", 0)
    log.info("Wrote %d cells to '%s'.", updated, SHEET_NAME)


def deduplicate_rows(sheets_service):
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A:C")
        .execute()
        .get("values", [])
    )
    if len(values) <= 2:
        return

    seen = set()
    duplicate_row_indexes = []
    for row_index, row in enumerate(values[1:], start=1):
        dedup_key = tuple(row[column_index] if column_index < len(row) else ""
                  for column_index in (0, 1, 2))
        if dedup_key in seen:
            duplicate_row_indexes.append(row_index)
        else:
            seen.add(dedup_key)

    if not duplicate_row_indexes:
        return

    duplicate_ranges = []
    range_start = range_end = duplicate_row_indexes[0]
    for row_index in duplicate_row_indexes[1:]:
        if row_index == range_end + 1:
            range_end = row_index
        else:
            duplicate_ranges.append((range_start, range_end + 1))
            range_start = range_end = row_index
    duplicate_ranges.append((range_start, range_end + 1))

    sheet_id = get_sheet_id(sheets_service, SHEET_NAME)
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_index,
                    "endIndex": end_index,
                }
            }
        }
        for start_index, end_index in reversed(duplicate_ranges)
    ]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()
    log.info("Removed %d duplicate row(s) from '%s'.", len(duplicate_row_indexes), SHEET_NAME)


def get_sheet_id(sheets_service, sheet_name: str) -> int:
    meta = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Sheet '{sheet_name}' not found in spreadsheet.")


def sort_by_purchase_date(sheets_service):
    sheet_id = get_sheet_id(sheets_service, SHEET_NAME)

    # Find how many rows currently have data (mirrors getLastDataRow's
    # "skip stray formatting" behavior closely enough for normal use).
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A:A")
        .execute()
        .get("values", [])
    )
    last_row = len(values)
    if last_row <= 1:
        return  # header only, nothing to sort

    request = {
        "sortRange": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,  # skip header
                "endRowIndex": last_row,
                "startColumnIndex": 0,
                "endColumnIndex": NUM_COLUMNS,
            },
            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
        }
    }
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": [request]}
    ).execute()
    log.info("Re-sorted '%s' by Purchase Date.", SHEET_NAME)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) != 3:
        print("Usage: python track_expenses.py <parser_type> /path/to/statement.pdf")
        sys.exit(1)

    parser_type = sys.argv[1]
    if parser_type != "nlb" and parser_type != "otp":
        log.error("Unsupported parser type: %s (supported: nlb, otp)", parser_type)
        sys.exit(1)

    statement_path = Path(sys.argv[2])
    if not statement_path.exists():
        log.error("Statement file not found: %s", statement_path)
        sys.exit(1)

    log.info("Parsing statement: %s", statement_path)
    expenses_csv, review_csv, statement_id = parse_statement(parser_type, statement_path)

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    sheets_service = build("sheets", "v4", credentials=creds)

    processed = load_processed()
    if statement_id in processed:
        log.info("Statement %s already imported, skipping.", statement_id)
        deduplicate_rows(sheets_service)
        return

    expense_rows = csv_to_rows(expenses_csv)
    review_rows = csv_to_rows(review_csv)

    append_rows(sheets_service, expense_rows + review_rows)
    deduplicate_rows(sheets_service)
    if expense_rows or review_rows:
        sort_by_purchase_date(sheets_service)

    processed.add(statement_id)
    save_processed(processed)

    log.info(
        "Done. Imported %d expense row(s) and %d review row(s) for %s.",
        len(expense_rows), len(review_rows), statement_id,
    )


if __name__ == "__main__":
    main()