"""Bank statement parser implementations and dispatch."""
from . import nlb, otp

PARSERS = {"nlb": nlb.extract_transactions, "otp": otp.extract_transactions}


def parse_statement(pdf_bytes: bytes, parser_type: str):
	try:
		parser = PARSERS[parser_type]
	except KeyError as error:
		raise ValueError(f"Unknown parser type: {parser_type}") from error
	return parser(pdf_bytes)


__all__ = ["PARSERS", "parse_statement"]
