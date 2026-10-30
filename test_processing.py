import os
import json
import tempfile
import unittest

from account_rotation import select_account
from main import run_browser_send
from retry_link import process_manual_retry
from processing_state import load_failures, load_successes


class FakeSender:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, link):
        self.calls.append(link)
        return self.outcomes.pop(0) if self.outcomes else "ok"


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = self.temp.name
        self.links = [f"https://t.me/muen2025/{number}" for number in range(1, 8)]
        with open(os.path.join(self.state, "telegram_links.txt"), "w", encoding="utf-8") as file:
            file.write("\n".join(self.links) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_each_run_is_limited_to_five_requests_and_five_successes_accumulate(self):
        sender = FakeSender(["ok"] * 10)
        self.assertEqual(run_browser_send(sender, self.state), 0)
        self.assertEqual(len(sender.calls), 5)
        self.assertEqual(load_successes(self.state)[self.links[0]], 1)
        second = FakeSender(["ok"] * 5)
        self.assertEqual(run_browser_send(second, self.state), 0)
        self.assertEqual(len(second.calls), 5)
        self.assertEqual(load_successes(self.state)[self.links[0]], 2)
        self.assertEqual(load_successes(self.state)[self.links[4]], 2)
        self.assertNotIn(self.links[5], load_successes(self.state))

    def test_failures_do_not_increment_success_and_third_failure_skips_link(self):
        sender = FakeSender(["fail"] * 5)
        run_browser_send(sender, self.state)
        self.assertEqual(load_successes(self.state), {})
        self.assertEqual(load_failures(self.state)[self.links[0]], 1)
        run_browser_send(FakeSender(["fail"] * 5), self.state)
        run_browser_send(FakeSender(["fail"] * 5), self.state)
        self.assertEqual(load_failures(self.state)[self.links[0]], 3)
        next_sender = FakeSender(["ok"] * 5)
        run_browser_send(next_sender, self.state)
        self.assertNotIn(self.links[0], next_sender.calls)

    def test_manual_retry_uses_shared_counter_and_rejects_at_five(self):
        for _ in range(4):
            self.assertEqual(process_manual_retry(self.links[0], FakeSender(["ok"]), self.state), 0)
        self.assertEqual(load_successes(self.state)[self.links[0]], 4)
        self.assertEqual(process_manual_retry(self.links[0], FakeSender(["ok"]), self.state), 0)
        blocked = FakeSender(["ok"])
        self.assertEqual(process_manual_retry(self.links[0], blocked, self.state), 2)
        self.assertEqual(blocked.calls, [])
        self.assertEqual(load_successes(self.state)[self.links[0]], 5)

    def test_legacy_done_links_remain_complete_without_rewriting_legacy_log(self):
        done_path = os.path.join(self.state, "done_links.txt")
        with open(done_path, "w", encoding="utf-8") as file:
            file.write(self.links[0] + "\n")
        with open(done_path, "rb") as file:
            before = file.read()
        sender = FakeSender(["ok"] * 5)
        run_browser_send(sender, self.state)
        self.assertNotIn(self.links[0], sender.calls)
        self.assertNotIn(self.links[0], load_successes(self.state))
        with open(done_path, "rb") as file:
            self.assertEqual(file.read(), before)
        blocked = FakeSender(["ok"])
        self.assertEqual(process_manual_retry(self.links[0], blocked, self.state), 2)
        self.assertEqual(blocked.calls, [])

    def test_account_rotation_is_shared_and_persists_1_2_3_1(self):
        env = {
            "SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": "pass-1",
            "SITE_USERNAME_2": "user-2", "SITE_PASSWORD_2": "pass-2",
            "SITE_USERNAME_3": "user-3", "SITE_PASSWORD_3": "pass-3",
        }
        selected = [select_account(self.state, env) for _ in range(4)]
        self.assertEqual([item[0] for item in selected], [1, 2, 3, 1])
        self.assertEqual(selected[0][1:], ("user-1", "pass-1"))
        with open(os.path.join(self.state, "account_rotation.json"), encoding="utf-8") as file:
            self.assertEqual(json.load(file), {"next_account": 2})

    def test_missing_secret_does_not_advance_or_expose_value(self):
        env = {"SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": ""}
        with self.assertRaisesRegex(RuntimeError, "الحساب 1") as context:
            select_account(self.state, env)
        self.assertNotIn("user-1", str(context.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, "account_rotation.json")))

    def test_corrupt_rotation_state_recovers_at_account_one(self):
        path = os.path.join(self.state, "account_rotation.json")
        with open(path, "w", encoding="utf-8") as file:
            file.write("not-json")
        account, username, password = select_account(
            self.state,
            {"SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": "pass-1"},
        )
        self.assertEqual((account, username, password), (1, "user-1", "pass-1"))
        with open(path, encoding="utf-8") as file:
            self.assertEqual(json.load(file), {"next_account": 2})


if __name__ == "__main__":
    unittest.main()
