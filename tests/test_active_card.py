from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.broker import invalidate_active_card, refresh_active_card, refresh_for_control_action


class ActiveCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".ack").mkdir()
        (self.root / ".ack/AXIOM-ACTIVE.md").write_text("AXIOM ACTIVE RULES\n\ncard\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_start_or_resume_invalidation_refreshes_once(self):
        self.assertTrue(refresh_active_card(self.root, now=0))
        self.assertFalse(refresh_active_card(self.root, now=1))
        invalidate_active_card(self.root)
        self.assertTrue(refresh_active_card(self.root, now=2))

    def test_dispatch_and_integration_actions_always_refresh(self):
        refresh_active_card(self.root, now=0)
        self.assertTrue(refresh_for_control_action(self.root, "ack_worker_run", now=1))
        self.assertTrue(refresh_for_control_action(self.root, "ack_worker_integrate", now=2))

    def test_recent_ordinary_control_call_does_not_refresh_but_stale_one_does(self):
        refresh_active_card(self.root, now=0)
        self.assertFalse(refresh_for_control_action(self.root, "ack_worker_reconcile", now=899))
        self.assertTrue(refresh_for_control_action(self.root, "ack_worker_reconcile", now=901))

    def test_refresh_only_reads_the_card(self):
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertTrue(refresh_active_card(self.root, now=0))
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
