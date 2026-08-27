from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ack/lib"))

from ack.runner import worker_guard_classification
from ack.control import ControlPlane
from tests.fakes import FakeRedis


class WorkerGuardTests(unittest.TestCase):
    def classify(self, **overrides):
        values = {"elapsed_seconds": 30, "progress_age_seconds": 30, "no_progress_seconds": 600, "max_worker_seconds": 1800}
        values.update(overrides)
        return worker_guard_classification(**values)

    def test_fresh_heartbeat_with_stale_progress_is_alive_but_stalled(self):
        result = self.classify(progress_age_seconds=601)
        self.assertEqual(result["classification"], "alive_but_stalled")
        self.assertFalse(result["stop"])

    def test_stale_progress_alone_is_not_terminal_failure(self):
        result = self.classify(progress_age_seconds=601)
        self.assertNotEqual(result["classification"], "failed")
        self.assertFalse(result["stop"])

    def test_increasing_usage_without_material_evidence_is_probable_nonproductive_execution(self):
        result = self.classify(progress_age_seconds=601, usage_tokens=1000, usage_increasing=True)
        self.assertEqual(result["classification"], "probable_nonproductive_execution")
        self.assertFalse(result["stop"])

    def test_wall_clock_ceiling_stops_even_without_cost_metadata(self):
        result = self.classify(elapsed_seconds=1800)
        self.assertEqual(result["classification"], "wall_time_ceiling")
        self.assertTrue(result["stop"])

    def test_token_and_cost_ceilings_stop_when_available(self):
        self.assertTrue(self.classify(usage_tokens=100, max_worker_tokens=100)["stop"])
        result = self.classify(usage_cost_usd=1.0, max_worker_cost_usd=1.0)
        self.assertEqual(result["classification"], "resource_ceiling")
        self.assertTrue(result["stop"])

    def test_material_evidence_does_not_get_called_probable_loop(self):
        result = self.classify(progress_age_seconds=601, usage_tokens=1000, usage_increasing=True, material_evidence=True)
        self.assertEqual(result["classification"], "alive_but_stalled")

    def test_guard_preserves_live_lease_and_does_not_redispatch(self):
        redis = FakeRedis()
        plane = ControlPlane(redis, "project")
        task = {"id": "TASK", "role": "builder", "model": "trinity-fast", "base_commit": "base", "worktree": "wt"}
        plane.start(task, "agent", "token", 60)
        progress = redis.hgetall(plane.agent_key("agent"))["progress_at_utc"]
        plane.guard("TASK", "agent", "token", "alive_but_stalled", "progress stale")
        row = redis.hgetall(plane.task_key("TASK"))
        self.assertEqual(redis.get(plane.lease_key("TASK")), "token")
        self.assertEqual(redis.hgetall(plane.agent_key("agent"))["progress_at_utc"], progress)
        self.assertEqual(row["health"], "alive_but_stalled")
        self.assertEqual(len(redis.streams[plane.events_key]), 2)


if __name__ == "__main__":
    unittest.main()
