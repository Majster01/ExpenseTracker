from pathlib import Path
import unittest

from backend.parsers import otp


class OtpMetadataTests(unittest.TestCase):
    def test_recipient_metadata_uses_the_complete_visual_line(self):
        words = [
            (155, 100, 190, 110, "Prejemnik:", 0, 0, 0),
            (191, 100, 230, 110, "ALDIJANA", 0, 0, 1),
            (231, 100, 240, 110, "M.", 0, 0, 2),
            (155, 112, 180, 122, "address", 0, 0, 0),
        ]

        self.assertEqual(otp.recipient_metadata(words), "Prejemnik: ALDIJANA M.")

    def test_ignores_payer_only_blocks(self):
        words = [
            (155, 100, 185, 110, "Plačnik:", 0, 0, 0),
            (186, 100, 220, 110, "PERSON", 0, 0, 1),
        ]

        self.assertIsNone(otp.recipient_metadata(words))

    def test_3292_pdf_contains_only_matching_recipient_lines(self):
        pdf_path = Path(__file__).resolve().parents[1] / "3292.PDF"
        transactions = otp.extract_transactions(pdf_path.read_bytes())
        metadata = [item["metadata"] for item in transactions if item["metadata"]]

        self.assertEqual(len(transactions), 31)
        self.assertEqual(len(metadata), 7)
        self.assertIn("Prejemnik: JP LPT D.O.O. SI56 0292", metadata)
        self.assertIn("Prejemnik: ALDIJANA M.", metadata)
        self.assertTrue(all(value.startswith("Prejemnik: ") for value in metadata))
        self.assertEqual(sum(item["metadata"] is None for item in transactions), 24)


if __name__ == "__main__":
    unittest.main()