"""In-memory statement processing and Google Sheets orchestration."""

import logging
from pathlib import Path
from typing import Optional

from . import parsers
from .parsers.base import ddmmyy_to_iso
from .parsers import base as parser_base
from .rules import RulesRepository

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SPREADSHEET_ID = "1JlIH41lNNVPEa3WJa9E7mZP5q3yY5YHm52vdJVK2PnI"
# TODO Change back to "Expenses Override"
SHEET_NAME = "Expenses Override Test"
NUM_COLUMNS = 6  # Purchase Date, Item, Amount, Category, Needs Review, Meta

WORK_DIR = Path("./output")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Parse the bank statement -> expenses_YYYY-MM.csv, review_YYYY-MM.csv
# ---------------------------------------------------------------------------
def parse_statement(
    parser_type: str,
    pdf_bytes: bytes,
    rules_path: Optional[Path] = None,
):
    """Parse uploaded PDF bytes and return transactions plus a statement ID."""
    transactions = parsers.parse_statement(pdf_bytes, parser_type)
    debits = [transaction for transaction in transactions if transaction["amount"] < 0]
    month_tag = (
        ddmmyy_to_iso(debits[0]["date"])[:7]
        if debits else "unknown"
    )
    return transactions, f"{parser_type}:{month_tag}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 2: Load a CSV and map to sheet row shape
#          CSV columns: date, amount, category, description
#          Sheet columns: Purchase Date, Item, Amount, Category, Needs Review
# ---------------------------------------------------------------------------
 # ---------------------------------------------------------------------------
 # Google Sheets operations
 # ---------------------------------------------------------------------------
def append_rows(sheets_service, rows: list[list]):
    if not rows:
        log.info("No rows to write, skipping.")
        return 0
    result = (
        sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:F1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        )
        .execute()
    )
    updated = result.get("updates", {}).get("updatedCells", 0)
    log.info("Wrote %d cells to '%s'.", updated, SHEET_NAME)
    return len(rows)


def deduplicate_rows(sheets_service):
    values = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A:C")
        .execute()
        .get("values", [])
    )
    if len(values) <= 2:
        return 0

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
        return 0

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
    return len(duplicate_row_indexes)


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
        return False  # header only, nothing to sort

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
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_and_track_statement(
    parser_type: str,
    pdf_bytes: bytes,
    sheets_service,
    rules_path: Optional[Path] = None,
    rules_repository: Optional[RulesRepository] = None,
):
    """Process one statement and update Google Sheets."""
    transactions, statement_id = parse_statement(parser_type, pdf_bytes, rules_path)

    rules = (rules_repository or RulesRepository(rules_path=rules_path)).get_rules()
    expense_rows = []
    review_rows = []
    credit_count = 0
    for transaction in transactions:
        if transaction["amount"] >= 0:
            credit_count += 1
            continue
        category, _keyword = parser_base.categorize(
            transaction["description"], rules
        )
        row = [
            ddmmyy_to_iso(transaction["date"]),
            transaction["description"],
            -transaction["amount"],
            category or "",
            "",
            transaction.get("metadata") or "",
        ]
        (expense_rows if category else review_rows).append(row)
    rows_added = append_rows(sheets_service, expense_rows + review_rows)
    duplicates_removed = deduplicate_rows(sheets_service)
    sorted_rows = sort_by_purchase_date(sheets_service) if rows_added else False

    return {
        "parser_type": parser_type,
        "statement_id": statement_id,
        "transactions_found": len(transactions),
        "expenses_found": len(expense_rows) + len(review_rows),
        "credits_found": credit_count,
        "matched_rows": len(expense_rows),
        "needs_categorization": len(review_rows),
        "rows_added": rows_added,
        "duplicates_removed": duplicates_removed,
        "sorted": sorted_rows,
    }


