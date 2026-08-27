"""Shared parser utilities and transaction helpers."""
import json
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional

DATE_FORMAT = "%d.%m.%Y"
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
AMOUNT_RE = re.compile(r"[+-]?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}")


def parse_amount(raw: str) -> float:
    sign = -1 if raw.strip().startswith("-") else 1
    number = raw.strip().lstrip("+-").replace(".", "").replace(",", ".")
    return sign * float(number)


def word_text(words: Iterable[Any]) -> str:
    return " ".join(" ".join(str(word[4]).split()) for word in words).strip()


def compact_text(words: Iterable[Any]) -> str:
    return re.sub(r"\s+", "", word_text(words))


def words_in_column(words: Iterable[Any], start_x: float, end_x: float):
    return [word for word in words if start_x <= word[0] < end_x]


def ddmmyy_to_iso(date_value: str) -> str:
    day, month, year = date_value.split(".")
    normalized_year = year if len(year) == 4 else "20" + year
    return f"{normalized_year}-{month}-{day}"


def transaction(
    date_value: str,
    description: str,
    amount: float,
    metadata: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "date": date_value,
        "description": description,
        "amount": amount,
        "metadata": metadata,
    }


def load_rules(rules_path: str):
    with open(rules_path, encoding="utf-8") as rules_file:
        rules = json.load(rules_file, object_pairs_hook=OrderedDict)
    return normalize_rules(rules)


def normalize_rules(rules):
    return OrderedDict(
        (category, [keyword.lower() for keyword in keywords])
        for category, keywords in rules.items()
    )


def categorize(description: str, rules):
    description_lower = description.lower()
    for category, keywords in rules.items():
        for keyword in keywords:
            if keyword and keyword in description_lower:
                return category, keyword
    return None, None
