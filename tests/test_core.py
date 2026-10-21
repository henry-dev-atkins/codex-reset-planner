import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from core import Observation, Settings, demo_usage, estimate_usage, optimize, scan_codex

UTC = timezone.utc


class LocalDataTests(unittest.TestCase):
    def test_scan_valid_rate_limit_rows_without_looking_at_auth(self):
        now = datetime.now(UTC).replace(microsecond=0)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / 'sessions' / '2026' / '10' / '21' / 'rollout.jsonl'
            log.parent.mkdir(parents=True)
            (root/'auth.json').write_text('{"private_token":"DO_NOT_READ"}')
            week_end = now + timedelta(days=4)
            five_end = now + timedelta(hours=4)
            rows = [
                {'timestamp': (now-timedelta(minutes=45)).isoformat(), 'type': 'event_msg',
                 'payload': {'type':'token_count', 'rate_limits': {
                     'primary': {'used_percent': 22, 'resets_at': five_end.timestamp()},
                     'secondary': {'used_percent': 40, 'resets_at': week_end.timestamp()}}}},
                {'timestamp': (now-timedelta(minutes=15)).isoformat(), 'type': 'event_msg',
                 'payload': {'type':'token_count', 'rate_limits': {
                     'primary': {'used_percent': 27, 'resets_at': five_end.timestamp()},
                     'secondary': {'used_percent': 42, 'resets_at': week_end.timestamp()}}}},
                {'timestamp': now.isoformat(), 'type':'turn_context',
                 'payload': {'model':'gpt-model', 'user_text':'secret message should not matter'}},
                {'timestamp': now.isoformat(), 'type':'event_msg',
                 'payload': {'type':'user_message','message':'DO_NOT_READ'}},
            ]
            log.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
            result = scan_codex(root, now)
            self.assertEqual(len(result.observations), 2)
            self.assertEqual(result.latest.week_used, 42)
            self.assertEqual(result.latest.five_used, 27)
            self.assertEqual(result.model, 'gpt-model')
            self.assertAlmostEqual(result.ratio, 2.5)
            self.assertIsNotNone(result.daily_rate)

    def test_no_fabricated_quota_when_folder_missing(self):
        result = scan_codex('/nonexistent-codex-folder-for-test')
        self.assertIsNone(result.latest)
        self.assertIsNone(result.daily_rate)

    def test_planner_never_consumes_more_than_demand_and_saved_count(self):
        now = datetime.now(UTC)
        obs = demo_usage(now).latest
        for mode in ('steady', 'spiky'):
            for count in (0, 1, 3):
                p = optimize(obs, now, 22, 4, count, mode)
                self.assertLessEqual(len(p.chosen), count)
                self.assertLessEqual(p.served_total, p.demand_total + .001)
                self.assertGreaterEqual(p.served_total + .001, p.baseline_total)
                self.assertTrue(all(0 <= r['end_week_used'] <= 100.0001 for r in p.daily_rows))
                self.assertTrue(all(0 <= r['end_five_used'] <= 100.0001 for r in p.daily_rows))

    def test_resets_adhere_to_expiry(self):
        now = datetime.now(UTC)
        p = optimize(demo_usage(now).latest, now, 30, 5, 3, 'spiky',
                     expiry=(now-timedelta(days=1)).date())
        self.assertFalse(p.chosen)

    def test_banked_reset_supersedes_old_reported_weekly_clock(self):
        now = datetime.now(UTC)
        obs = demo_usage(now).latest
        p = optimize(obs, now, 12, 3.7, 2, 'spiky')
        for redeemed in p.chosen:
            if redeemed <= obs.week_end:
                old = [e for e in p.events if e.get('observed')
                       and e['type'] == 'weekly' and e['ts'] == obs.week_end]
                self.assertEqual(old, [])

    def test_settings_roundtrip(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'config.json'
            s=Settings(banked=2, expiry='2026-11-15', daily_override=15.2)
            s.save(p)
            loaded=Settings.load(p)
            self.assertEqual(loaded.banked, 2)
            self.assertEqual(loaded.expiry, '2026-11-15')
            self.assertEqual(loaded.daily_override, 15.2)


if __name__=='__main__':
    unittest.main()
