import unittest
from unittest.mock import patch
from unittest.mock import Mock

from backend import processor


class DeduplicateRowsTests(unittest.TestCase):
    def test_removes_empty_category_duplicate_before_categorized_row(self):
        sheets_service = Mock()
        sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
            "values": [
                ["Purchase Date", "Item", "Amount", "Category"],
                ["2026-08-01", "Coffee Shop", "4.50", ""],
                ["2026-08-01", "Coffee Shop", "4.50", "Food"],
            ]
        }
        sheets_service.spreadsheets().get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": processor.SHEET_NAME, "sheetId": 42}}]
        }

        duplicates_removed = processor.deduplicate_rows(sheets_service)

        self.assertEqual(duplicates_removed, 1)
        sheets_service.spreadsheets().batchUpdate.assert_called_once_with(
            spreadsheetId=processor.SPREADSHEET_ID,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": 42,
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 2,
                            }
                        }
                    }
                ]
            },
        )


class AppendRowsTests(unittest.TestCase):
    def test_appends_rows_through_metadata_column(self):
        sheets_service = Mock()
        sheets_service.spreadsheets().values().append.return_value.execute.return_value = {
            "updates": {"updatedCells": 6}
        }

        rows_added = processor.append_rows(
            sheets_service,
            [["2026-08-01", "Coffee Shop", 4.5, "Food", "", "Prejemnik: Shop"]],
        )

        self.assertEqual(rows_added, 1)
        sheets_service.spreadsheets().values().append.assert_called_once_with(
            spreadsheetId=processor.SPREADSHEET_ID,
            range=f"{processor.SHEET_NAME}!A1:F1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [["2026-08-01", "Coffee Shop", 4.5, "Food", "", "Prejemnik: Shop"]]},
        )


class ProcessRowsTests(unittest.TestCase):
    def test_processes_metadata_into_column_f_and_pads_column_e(self):
        sheets_service = Mock()
        transaction = {
            "date": "01.08.2026",
            "description": "Coffee Shop",
            "amount": -4.5,
            "metadata": "Prejemnik: Shop",
        }

        with patch.object(processor, "parse_statement", return_value=([transaction], "otp:2026-08")), \
                patch.object(processor.parser_base, "load_rules", return_value={}), \
                patch.object(processor.parser_base, "categorize", return_value=("Food", "coffee")), \
                patch.object(processor, "append_rows", return_value=1) as append_rows, \
                patch.object(processor, "deduplicate_rows", return_value=0), \
                patch.object(processor, "sort_by_purchase_date", return_value=True):
            result = processor.process_and_track_statement("otp", b"pdf", sheets_service)

        self.assertEqual(result["rows_added"], 1)
        append_rows.assert_called_once_with(
            sheets_service,
            [["2026-08-01", "Coffee Shop", 4.5, "Food", "", "Prejemnik: Shop"]],
        )


class SortRowsTests(unittest.TestCase):
    def test_sort_range_includes_metadata_column(self):
        sheets_service = Mock()
        sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
            "values": [["Purchase Date"], ["2026-08-01"]]
        }
        with patch.object(processor, "get_sheet_id", return_value=42):
            self.assertTrue(processor.sort_by_purchase_date(sheets_service))

        sheets_service.spreadsheets().batchUpdate.assert_called_once_with(
            spreadsheetId=processor.SPREADSHEET_ID,
            body={
                "requests": [
                    {
                        "sortRange": {
                            "range": {
                                "sheetId": 42,
                                "startRowIndex": 1,
                                "endRowIndex": 2,
                                "startColumnIndex": 0,
                                "endColumnIndex": 6,
                            },
                            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
                        }
                    }
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()