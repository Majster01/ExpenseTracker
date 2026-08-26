"""OTP bank statement parser."""
from datetime import datetime

import fitz

from .base import AMOUNT_RE, DATE_FORMAT, DATE_RE, compact_text, parse_amount, transaction, word_text, words_in_column


def extract_transactions(pdf_bytes: bytes):
	document = fitz.open(stream=pdf_bytes, filetype="pdf")
	transactions = []
	try:
		for page in document:
			words = page.get_text("words")
			header_words = [word for word in words if compact_text([word]) == "Opis"]
			if not header_words:
				continue
			header_top = min(word[1] for word in header_words)
			date_words = [
				word for word in words
				if word[1] > header_top + 15
				and word[0] < 100
				and DATE_RE.fullmatch(compact_text([word]))
			]
			date_words.sort(key=lambda word: word[1])
			for date_index in range(0, len(date_words) - 1, 2):
				transaction_date = compact_text([date_words[date_index]])
				try:
					datetime.strptime(transaction_date, DATE_FORMAT)
				except ValueError:
					continue
				start_top = date_words[date_index][1]
				end_top = (
					date_words[date_index + 2][1]
					if date_index + 2 < len(date_words)
					else max(word[3] for word in words) + 1
				)
				block = [word for word in words if start_top <= word[1] < end_top]
				debit = AMOUNT_RE.search(compact_text(words_in_column(block, 285, 360)))
				credit = AMOUNT_RE.search(compact_text(words_in_column(block, 360, 430)))
				if debit:
					amount = -abs(parse_amount(debit.group()))
				elif credit:
					amount = abs(parse_amount(credit.group()))
				else:
					continue
				description_words = words_in_column(block, 440, 594)
				description_words.sort(key=lambda word: (word[1], word[0]))
				transactions.append(transaction(
					transaction_date, word_text(description_words), amount
				))
	finally:
		document.close()
	return transactions

__all__ = ["extract_transactions"]
