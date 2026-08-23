import unittest
from datetime import datetime, timedelta, timezone

from app.signal_worker import candidate_digest, eligible_fresh_candidates, terminal_http_status


class SignalWorkerTests(unittest.TestCase):
    def test_signal_id_makes_digest_stable_across_market_refreshes(self):
        first = {"signal_id": "abc", "source_timestamp": "2026-01-01T00:00:00+00:00", "price": 1}
        second = {"signal_id": "abc", "source_timestamp": "2026-01-01T00:00:10+00:00", "price": 2}
        self.assertEqual(candidate_digest(first), candidate_digest(second))

    def test_only_forwards_fresh_candidates(self):
        now=datetime.now(timezone.utc)
        fresh={"product_id":"AAA-USDC","source_timestamp":now.isoformat()}
        stale={"product_id":"BBB-USDC","source_timestamp":(now-timedelta(minutes=5)).isoformat()}
        self.assertEqual(eligible_fresh_candidates({"candidates":[fresh,stale]}),[fresh])

    def test_digest_is_stable(self):
        self.assertEqual(candidate_digest({"b":2,"a":1}),candidate_digest({"a":1,"b":2}))

    def test_invalid_feed_shape_fails_closed(self):
        with self.assertRaises(ValueError):eligible_fresh_candidates({"candidate":{}})

    def test_transient_executor_errors_are_retryable(self):
        self.assertFalse(terminal_http_status(500))
        self.assertFalse(terminal_http_status(503))
        self.assertTrue(terminal_http_status(422))


if __name__ == "__main__":unittest.main()
