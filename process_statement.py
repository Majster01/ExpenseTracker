#!/usr/bin/env python3
"""
Process an NLB bank statement PDF into a categorized expense CSV.

Usage:
    python3 process_statement.py <statement.pdf> [--rules category_rules.json] [--out out_dir]

Produces two files in the output dir:
    expenses_<month>.csv   - Date, Amount, Category, Description  (ready to import)
    review_<month>.csv     - transactions that didn't confidently match a rule,
                              for you to categorize manually / add new keywords

Design notes:
- Category rules live in a JSON file (category -> list of lowercase substrings),
  so adding a new category or keyword is a one-line edit, no code changes.
- Only DEBIT (expense) transactions are categorized. Credits (salary, refunds)
  are written to a separate credits_<month>.csv for visibility, not silently dropped.
- Bank fees ("provizija", account package fees) are treated as Bills by default
  since they're recurring account costs - move them in the rules file if you'd
  rather have a "Fees" category.
"""
import re
import sys
import csv
import json
import argparse
import subprocess
from html.parser import HTMLParser
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

LINE_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{2})\s+(?P<rest>.*?)\s+(?P<amount>[+-]\d[\d.]*,\d{2})\s+(?P<balance>[+-]?\d[\d.]*,\d{2})\s*$"
)
# The "rest" group can contain the merchant name AND the recipient-account column
# (separated by 2+ spaces, since pdftotext -layout preserves column gaps).
# Keep only the merchant/description column.
COLUMN_SPLIT_RE = re.compile(r"\s{2,}")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
DATE_FORMAT = "%d.%m.%Y"
AMOUNT_RE = re.compile(r"[+-]?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}")

def parse_amount(raw: str) -> float:
    # "1.234,56" -> 1234.56 ; keep sign
    sign = -1 if raw.strip().startswith("-") else 1
    num = raw.strip().lstrip("+-").replace(".", "").replace(",", ".")
    return sign * float(num)

class _BBoxParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pages = [[]]
        self.current_word = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "page":
            self.pages.append([])
        elif tag == "word":
            self.current_word = {
                "x_min": float(attributes["xmin"]),
                "x_max": float(attributes["xmax"]),
                "y_min": float(attributes["ymin"]),
                "y_max": float(attributes["ymax"]),
                "text": "",
            }

    def handle_data(self, data):
        if self.current_word is not None:
            self.current_word["text"] += data

    def handle_endtag(self, tag):
        if tag == "word" and self.current_word is not None:
            self.pages[-1].append(self.current_word)
            self.current_word = None


def _word_text(words):
    return " ".join(" ".join(word["text"].split()) for word in words).strip()


def _compact_text(words):
    return re.sub(r"\s+", "", _word_text(words))


def _words_in_column(words, start_x, end_x):
    return [word for word in words if word["x_min"] >= start_x and word["x_min"] < end_x]


def extract_transactions_otp(pdf_path: str):
    bbox_text = subprocess.run(
        ["pdftotext", "-bbox-layout", pdf_path, "-"],
        capture_output=True, text=True, check=True
    ).stdout
    bbox = _BBoxParser()
    bbox.feed(bbox_text)

    transactions = []
    for words in bbox.pages:
        header_words = [word for word in words if _compact_text([word]) == "Opis"]
        if not header_words:
            continue
        header_y = min(word["y_min"] for word in header_words)

        date_words = [
            word for word in words
            if word["y_min"] > header_y + 15 and word["x_min"] < 100
            and DATE_RE.fullmatch(_compact_text([word]))
        ]
        date_words.sort(key=lambda word: word["y_min"])

        # OTP prints booking and value dates on consecutive lines. Each pair
        # defines one transaction block; the first date is the booking date.
        for date_index in range(0, len(date_words) - 1, 2):
            transaction_date = _compact_text([date_words[date_index]])
            if not DATE_RE.fullmatch(transaction_date):
                continue
            try:
                datetime.strptime(transaction_date, DATE_FORMAT)
            except ValueError:
                continue

            start_y = date_words[date_index]["y_min"]
            end_y = (
                date_words[date_index + 2]["y_min"]
                if date_index + 2 < len(date_words)
                else max(word["y_max"] for word in words) + 1
            )
            block = [
                word for word in words
                if start_y <= word["y_min"] < end_y
            ]

            debit_words = _words_in_column(block, 285, 360)
            credit_words = _words_in_column(block, 360, 430)
            debit = AMOUNT_RE.search(_compact_text(debit_words))
            credit = AMOUNT_RE.search(_compact_text(credit_words))
            if debit:
                amount = -abs(parse_amount(debit.group()))
            elif credit:
                amount = abs(parse_amount(credit.group()))
            else:
                continue

            description = _word_text(_words_in_column(block, 440, 594))

            transactions.append({
                "date": transaction_date,
                "description": description,
                "amount": amount,
            })
    return transactions

def extract_transactions_nlb(pdf_path: str):
    txt = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, check=True
    ).stdout
    transactions = []
    for raw_line in txt.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("Datum", "Namen/opis", "Račun prejemnika", "EUR - EVRO")):
            continue
        if any(k in line for k in ["STANJE PREDHODNEGA", "SKUPNI PROMET", "NOVO STANJE", "Napoved spremembe"]):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue  # continuation lines (payment purpose / reference) - description enrichment could be added here
        date = m.group("date")
        # "rest" may be "MERCHANT NAME    SI56 0400 ..." (account col) - keep first column only
        desc = COLUMN_SPLIT_RE.split(m.group("rest").strip())[0].strip()
        amount = parse_amount(m.group("amount"))
        transactions.append({"date": date, "description": desc, "amount": amount})
    return transactions

def load_rules(rules_path: str) -> "OrderedDict[str, list]":
    with open(rules_path, encoding="utf-8") as f:
        rules = json.load(f, object_pairs_hook=OrderedDict)
    return OrderedDict((cat, [kw.lower() for kw in kws]) for cat, kws in rules.items())

def categorize(description: str, rules: "OrderedDict[str, list]"):
    desc_lower = description.lower()
    for category, keywords in rules.items():
        for kw in keywords:
            if kw and kw in desc_lower:
                return category, kw
    return None, None

def ddmmyy_to_iso(d: str) -> str:
    dd, mm, yy = d.split(".")
    year = yy if len(yy) == 4 else f"20{yy}"
    return f"{year}-{mm}-{dd}"

def main(argv=None, parser_type="nlb"):
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--rules", default="category_rules.json")
    ap.add_argument("--out", default=".")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rules = load_rules(args.rules)
    if parser_type == "nlb":
        transactions = extract_transactions_nlb(args.pdf)
    elif parser_type == "otp":
        transactions = extract_transactions_otp(args.pdf)
    else:
        raise ValueError(f"Unknown parser type: {parser_type}")

    debits = [t for t in transactions if t["amount"] < 0]
    credits = [t for t in transactions if t["amount"] >= 0]

    if debits:
        month_tag = ddmmyy_to_iso(debits[0]["date"])[:7]
    else:
        month_tag = "unknown"

    expenses_path = out_dir / f"expenses_{parser_type}_{month_tag}.csv"
    review_path = out_dir / f"review_{parser_type}_{month_tag}.csv"
    credits_path = out_dir / f"credits_{parser_type}_{month_tag}.csv"

    matched, unmatched = [], []
    for t in debits:
        cat, kw = categorize(t["description"], rules)
        row = {
            "date": ddmmyy_to_iso(t["date"]),
            "amount": f"{-t['amount']:.2f}",  # positive = expense amount in EUR
            "category": cat or "",
            "description": t["description"],
        }
        (matched if cat else unmatched).append(row)

    with open(expenses_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "amount", "category", "description"])
        w.writeheader()
        w.writerows(matched)

    with open(review_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "amount", "category", "description"])
        w.writeheader()
        w.writerows(unmatched)

    with open(credits_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "amount", "description"])
        w.writeheader()
        for t in credits:
            w.writerow({"date": ddmmyy_to_iso(t["date"]), "amount": f"{t['amount']:.2f}", "description": t["description"]})

    print(f"Parsed {len(transactions)} lines -> {len(debits)} debits, {len(credits)} credits")
    print(f"Matched: {len(matched)}  Unmatched (needs review): {len(unmatched)}")
    print(f"Wrote: {expenses_path}, {review_path}, {credits_path}")
    return expenses_path, review_path, credits_path, month_tag

if __name__ == "__main__":
    main()
