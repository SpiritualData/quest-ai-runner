"""Offline test: world.diff() sees experiment adds, edits and removals (no network, no world)."""
import unittest

import world as W


def snap(experiments):
    return {"quests": {"fitness": {"notes": {}, "goals": {}, "experiments": experiments}},
            "collections": {}, "all_quest_ids": [], "all_collection_ids": [], "tasks": {}}


class ExperimentDiffTest(unittest.TestCase):
    base = {"rq_1": {"question": "default", "status": "open", "kind": "default"}}

    def test_added(self):
        after = dict(self.base, rq_2={"question": "does coffee help my pace?", "status": "open"})
        changes = W.diff(snap(self.base), snap(after))
        self.assertEqual(len(changes), 1)
        self.assertIn("EXPERIMENT ADDED", changes[0])

    def test_edited(self):
        after = {"rq_1": dict(self.base["rq_1"], conclusion="it helped")}
        self.assertIn("EXPERIMENT CHANGED", W.diff(snap(self.base), snap(after))[0])

    def test_removed(self):
        self.assertIn("EXPERIMENT REMOVED", W.diff(snap(self.base), snap({}))[0])

    def test_unchanged(self):
        self.assertEqual(W.diff(snap(self.base), snap(dict(self.base))), [])


if __name__ == "__main__":
    unittest.main()
