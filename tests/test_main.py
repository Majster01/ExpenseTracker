import asyncio
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, UploadFile
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from backend import main


class MainAuthTests(unittest.TestCase):
    def test_require_admin_accepts_allowlisted_email(self):
        with patch.object(main, "ADMIN_EMAILS", {"admin@example.com"}), patch.object(
            main, "_get_session", return_value={"email": "Admin@Example.com"}
        ):
            self.assertEqual(main._require_admin("session-id")["email"], "Admin@Example.com")

    def test_require_admin_rejects_non_allowlisted_email(self):
        with patch.object(main, "ADMIN_EMAILS", {"admin@example.com"}), patch.object(
            main, "_get_session", return_value={"email": "user@example.com"}
        ):
            with self.assertRaises(HTTPException) as context:
                main._require_admin("session-id")

        self.assertEqual(context.exception.status_code, 403)

    def test_save_rule_rejects_negative_order(self):
        with patch.object(main, "_require_admin"), patch.object(main, "_rules_repository") as repository:
            with self.assertRaises(HTTPException) as context:
                main.save_rule(main.RuleRequest(keywords=["wolt"], order=-1), "Food", "session-id")

        self.assertEqual(context.exception.status_code, 422)
        repository.return_value.save.assert_not_called()

    def test_google_error_reason_extracts_reason_without_response_dump(self):
        error = HttpError(
            Mock(status=403),
            b'{"error":{"errors":[{"reason":"insufficientPermissions"}]}}',
        )

        self.assertEqual(main._google_error_reason(error), "insufficientPermissions")

    def test_require_google_user_rejects_missing_oauth_configuration(self):
        with patch.object(main, "GOOGLE_CLIENT_ID", None):
            with self.assertRaises(HTTPException) as context:
                main._require_google_user("Bearer caller-access-token")

        self.assertEqual(context.exception.status_code, 500)

    def test_require_google_user_builds_and_validates_caller_credentials(self):
        sheets_service = Mock()
        sheets_service.spreadsheets().get.return_value.execute.return_value = {
            "spreadsheetId": main.processor.SPREADSHEET_ID
        }
        caller_credentials = object()

        with patch.object(main, "GOOGLE_CLIENT_ID", "client-id"), patch.object(
            main.user_credentials, "Credentials", return_value=caller_credentials
        ) as credentials_factory, patch.object(
            main, "build", return_value=sheets_service
        ) as build_service:
            result = main._require_google_user("Bearer caller-access-token")

        self.assertIs(result, sheets_service)
        credentials_factory.assert_called_once_with(
            token="caller-access-token", scopes=main.SCOPES
        )
        build_service.assert_called_once_with(
            "sheets", "v4", credentials=caller_credentials
        )
        sheets_service.spreadsheets().get.assert_called_once_with(
            spreadsheetId=main.processor.SPREADSHEET_ID,
            fields="spreadsheetId",
        )

    def test_require_google_user_rejects_missing_token(self):
        with patch.object(main, "GOOGLE_CLIENT_ID", "client-id"):
            with self.assertRaises(HTTPException) as context:
                main._require_google_user(None)

        self.assertEqual(context.exception.status_code, 401)

    def test_require_google_user_rejects_empty_bearer_token(self):
        with patch.object(main, "GOOGLE_CLIENT_ID", "client-id"):
            with self.assertRaises(HTTPException) as context:
                main._require_google_user("Bearer   ")

        self.assertEqual(context.exception.status_code, 401)

    def test_require_google_user_maps_failed_token_refresh_to_401(self):
        sheets_service = Mock()
        sheets_service.spreadsheets().get.return_value.execute.side_effect = RefreshError(
            "The access token is invalid"
        )

        with patch.object(main, "GOOGLE_CLIENT_ID", "client-id"), patch.object(
            main.user_credentials, "Credentials"
        ), patch.object(main, "build", return_value=sheets_service):
            with self.assertRaises(HTTPException) as context:
                main._require_google_user("Bearer rejected-token")

        self.assertEqual(context.exception.status_code, 401)

    def test_upload_passes_caller_sheets_service_to_processor(self):
        sheets_service = object()
        result = {"rows_added": 1, "duplicates_removed": 0}
        upload = UploadFile(BytesIO(b"%PDF test"), filename="statement.pdf")

        with patch.object(main, "_require_google_user", return_value=sheets_service), patch.object(
            main.processor, "process_and_track_statement", return_value=result
        ) as process_statement:
            response = asyncio.run(main.upload_statement(upload, "nlb", "Bearer token"))

        self.assertEqual(response, result)
        process_statement.assert_called_once_with("nlb", b"%PDF test", sheets_service)


if __name__ == "__main__":
    unittest.main()