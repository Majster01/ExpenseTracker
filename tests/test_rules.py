import unittest
from collections import OrderedDict
from unittest.mock import Mock

from backend.rules import RulesRepository


class RulesRepositoryTests(unittest.TestCase):
    def test_empty_firestore_is_seeded_in_json_order(self):
        client = Mock()
        collection = client.collection.return_value
        collection.stream.return_value = []
        rules = OrderedDict([("Bills", ["Telekom"]), ("Food", ["Wolt"])])
        repository = RulesRepository(client)
        repository._load_json = Mock(return_value=rules)

        self.assertEqual(repository.get_rules(), rules)
        client.batch.return_value.commit.assert_called_once()

    def test_firestore_rules_are_sorted_by_order(self):
        client = Mock()
        snapshots = [
            Mock(to_dict=lambda: {"category": "Food", "keywords": ["Wolt"], "order": 1}),
            Mock(to_dict=lambda: {"category": "Bills", "keywords": ["Telekom"], "order": 0}),
        ]
        client.collection.return_value.stream.return_value = snapshots

        self.assertEqual(
            list(RulesRepository(client).get_rules().items()),
            [("Bills", ["telekom"]), ("Food", ["wolt"])],
        )

    def test_firestore_failure_uses_json_fallback(self):
        client = Mock()
        client.collection.return_value.stream.side_effect = RuntimeError("offline")
        repository = RulesRepository(client)
        fallback = OrderedDict([("Food", ["wolt"])])
        repository._load_json = Mock(return_value=fallback)

        self.assertEqual(repository.get_rules(), fallback)


if __name__ == "__main__":
    unittest.main()