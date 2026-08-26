import unittest
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


if __name__ == "__main__":
    unittest.main()