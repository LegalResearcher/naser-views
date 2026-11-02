import json
import os
import tempfile
import unittest
from unittest.mock import patch

import main
from processing_state import (
    load_account_history,
    load_failures,
    load_successes,
    mark_account_handled,
    save_successes,
)
from retry_link import process_manual_retry


class FakeSender:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def __call__(self, link):
        self.calls.append(link)
        return self.outcomes.pop(0) if self.outcomes else "ok"


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = self.temp.name
        self.links = [f"https://t.me/muen2025/{number}" for number in range(1, 8)]
        self.sleep_patch = patch("main.time.sleep", return_value=None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.temp.cleanup()

    def run_batch(self, sender, links=None, priority_links=None, account_number=1):
        return main.run_browser_send(
            sender, self.state, account_number, links, priority_links
        )

    def test_new_links_are_prioritized_newest_first_then_previous_batch(self):
        previous = [self.links[0], self.links[1]]
        with open(os.path.join(self.state, "previous_batch.json"), "w", encoding="utf-8") as file:
            json.dump(previous, file)
        batch = main.prepare_run_batch([self.links[2], self.links[0]], self.state)
        self.assertEqual(batch, [self.links[2], self.links[0], self.links[1]])
        sender = FakeSender(["ok"] * 5)
        self.assertEqual(
            self.run_batch(sender, batch, [self.links[2], self.links[0]]), 0
        )
        self.assertEqual(sender.calls, [self.links[2], self.links[0], self.links[1]])
        self.assertEqual(main.load_previous_batch(self.state), batch)

    def test_legacy_queue_migration_keeps_all_incomplete_links(self):
        with open(os.path.join(self.state, "telegram_links.txt"), "w", encoding="utf-8") as file:
            file.write("\n".join(self.links) + "\n")
        with open(os.path.join(self.state, "success_counts.json"), "w", encoding="utf-8") as file:
            json.dump({self.links[0]: 5}, file)
        self.assertEqual(main.load_previous_batch(self.state), self.links[1:])

    def test_only_new_links_are_discovered_once(self):
        found = main.discover_new_links(self.state, [self.links[0], self.links[1]])
        self.assertEqual(found, self.links[:2])
        self.assertEqual(
            main.discover_new_links(self.state, [self.links[0], self.links[1], self.links[2]]),
            [self.links[2]],
        )

    def test_untried_links_get_first_pass_before_newest_five_repeats(self):
        first = FakeSender(["ok"] * 5)
        self.assertEqual(self.run_batch(first, self.links), 0)
        self.assertEqual(first.calls, list(reversed(self.links[-5:])))

        untried = FakeSender(["ok"] * 5)
        self.assertEqual(self.run_batch(untried, self.links), 0)
        self.assertEqual(untried.calls, list(reversed(self.links[:2])))

        for _ in range(4):
            self.run_batch(FakeSender(["ok"] * 5), self.links)
        self.assertEqual(load_successes(self.state)[self.links[-1]], 5)

        older_group = FakeSender(["ok"] * 5)
        self.assertEqual(self.run_batch(older_group, self.links), 0)
        self.assertEqual(older_group.calls, list(reversed(self.links[:2])))

    def test_incomplete_newest_five_group_blocks_older_links(self):
        successes = {link: 1 for link in self.links}
        successes[self.links[-1]] = 5
        save_successes(self.state, successes)
        sender = FakeSender(["ok"] * 5)
        self.assertEqual(self.run_batch(sender, self.links), 0)
        self.assertEqual(sender.calls, list(reversed(self.links[-5:-1])))
        self.assertNotIn(self.links[1], sender.calls)

    def test_old_account_history_marks_link_as_previously_tried_but_does_not_block_it(self):
        mark_account_handled(self.links[-1], 1, self.state)
        self.assertEqual(load_account_history(self.state), {self.links[-1]: [1]})
        first_pass = main.pending_links(self.links, self.state)
        self.assertNotIn(self.links[-1], first_pass)

        sender = FakeSender(["ok"])
        self.assertEqual(self.run_batch(sender, [self.links[-1]], account_number=1), 0)
        self.assertEqual(sender.calls, [self.links[-1]])

    def test_failures_do_not_increment_success_and_three_failures_skip(self):
        self.run_batch(FakeSender(["fail"] * 5), self.links)
        self.assertEqual(load_successes(self.state), {})
        self.assertEqual(load_failures(self.state)[self.links[-1]], 1)

        # The remaining never-tried links receive their first attempt before retries.
        self.run_batch(FakeSender(["fail"] * 5), self.links)
        self.run_batch(FakeSender(["fail"] * 5), self.links)
        self.run_batch(FakeSender(["fail"] * 5), self.links)
        self.assertEqual(load_failures(self.state)[self.links[-1]], 3)

        later_group = FakeSender(["ok"] * 5)
        self.run_batch(later_group, self.links)
        self.assertNotIn(self.links[-1], later_group.calls)

    def test_manual_retry_uses_same_counters_and_rejects_at_five(self):
        for _ in range(4):
            self.assertEqual(
                process_manual_retry(self.links[0], FakeSender(["ok"]), self.state), 0
            )
        self.assertEqual(load_successes(self.state)[self.links[0]], 4)
        self.assertEqual(
            process_manual_retry(self.links[0], FakeSender(["ok"]), self.state), 0
        )
        blocked = FakeSender(["ok"])
        self.assertEqual(process_manual_retry(self.links[0], blocked, self.state), 2)
        self.assertEqual(blocked.calls, [])
        self.assertEqual(load_successes(self.state)[self.links[0]], 5)

    def test_legacy_done_link_is_never_retried(self):
        done_path = os.path.join(self.state, "done_links.txt")
        with open(done_path, "w", encoding="utf-8") as file:
            file.write(self.links[0] + "\n")
        with open(done_path, "rb") as file:
            original = file.read()
        sender = FakeSender(["ok"] * 5)
        self.run_batch(sender, self.links)
        self.assertNotIn(self.links[0], sender.calls)
        self.assertNotIn(self.links[0], load_successes(self.state))
        with open(done_path, "rb") as file:
            self.assertEqual(file.read(), original)
        blocked = FakeSender(["ok"])
        self.assertEqual(process_manual_retry(self.links[0], blocked, self.state), 2)
        self.assertEqual(blocked.calls, [])

    def test_fatal_stops_only_current_run_without_recording_order_failure(self):
        sender = FakeSender(["fatal"])
        self.assertEqual(self.run_batch(sender, self.links[:2]), 1)
        self.assertEqual(sender.calls, [self.links[1]])
        self.assertEqual(load_successes(self.state), {})
        self.assertEqual(load_failures(self.state), {})
        self.assertFalse(os.path.exists(os.path.join(self.state, "account_pause.json")))


if __name__ == "__main__":
    unittest.main()
