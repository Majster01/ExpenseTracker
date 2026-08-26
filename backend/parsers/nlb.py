"""NLB bank statement parser."""
import re

import fitz

from .base import parse_amount, transaction

LINE_RE = re.compile(
	r"^(?P<date>\d{2}\.\d{2}\.\d{2})\s+(?P<rest>.*?)\s+"
	r"(?P<amount>[+-]\d[\d.]*,\d{2})\s+(?P<balance>[+-]?[\d.]*,\d{2})\s*$"
)
COLUMN_SPLIT_RE = re.compile(r"\s{2,}")


def extract_transactions(pdf_bytes: bytes):
	document = fitz.open(stream=pdf_bytes, filetype="pdf")
	try:
		text = "\n".join(page.get_text("text") for page in document)
	finally:
		document.close()

	transactions = []
	for raw_line in text.splitlines():
		line = raw_line.strip()
		if not line or line.startswith(("Datum", "Namen/opis", "Račun prejemnika", "EUR - EVRO")):
			continue
		if any(value in line for value in [
			"STANJE PREDHODNEGA", "SKUPNI PROMET", "NOVO STANJE", "Napoved spremembe"
		]):
			continue
		match = LINE_RE.match(line)
		if not match:
			continue
		description = COLUMN_SPLIT_RE.split(match.group("rest").strip())[0].strip()
		transactions.append(transaction(
			match.group("date"), description, parse_amount(match.group("amount"))
		))
	return transactions

__all__ = ["extract_transactions"]
