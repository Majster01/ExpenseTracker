"""Firestore-backed category rules with a checked-in JSON fallback."""

import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .parsers.base import normalize_rules

log = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "category_rules.json"
DEFAULT_COLLECTION = "expense_tracker_rules"


class RulesRepository:
    def __init__(self, firestore_client=None, rules_path: Optional[Path] = None, collection_name: str = DEFAULT_COLLECTION):
        self.firestore_client = firestore_client
        self.rules_path = rules_path or DEFAULT_RULES_PATH
        self.collection_name = collection_name

    def _load_json(self) -> OrderedDict:
        with self.rules_path.open(encoding="utf-8") as rules_file:
            return normalize_rules(json.load(rules_file, object_pairs_hook=OrderedDict))

    def _collection(self):
        return self.firestore_client.collection(self.collection_name)

    def get_rules(self) -> OrderedDict:
        if self.firestore_client is None:
            return self._load_json()
        try:
            snapshots = list(self._collection().stream())
            if not snapshots:
                rules = self._load_json()
                self._seed(rules)
                return rules
            records = sorted(
                (snapshot.to_dict() for snapshot in snapshots),
                key=lambda record: (record.get("order", 0), record.get("category", "")),
            )
            return normalize_rules(OrderedDict(
                (record["category"], record.get("keywords", []))
                for record in records
            ))
        except Exception:
            log.exception("Could not load category rules from Firestore; using JSON fallback")
            return self._load_json()

    def _seed(self, rules: OrderedDict) -> None:
        batch = self.firestore_client.batch()
        for order, (category, keywords) in enumerate(rules.items()):
            batch.set(self._collection().document(category), {
                "category": category,
                "keywords": keywords,
                "order": order,
            })
        batch.commit()

    def save(self, category: str, keywords: list[str], order: int) -> None:
        self._collection().document(category).set({
            "category": category,
            "keywords": normalize_rules(OrderedDict([(category, keywords)]))[category],
            "order": order,
        })

    def delete(self, category: str) -> None:
        self._collection().document(category).delete()

    def list_rules(self) -> list[dict]:
        rules = self.get_rules()
        return [
            {"category": category, "keywords": keywords, "order": order}
            for order, (category, keywords) in enumerate(rules.items())
        ]