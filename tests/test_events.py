"""Tests for the event table (hookdoctor.events)."""

import unittest

from hookdoctor import events


class TestEvents(unittest.TestCase):
    def test_thirty_three_events(self):
        self.assertEqual(len(events.EVENTS), 33)
        self.assertEqual(len(set(events.EVENTS)), 33)

    def test_timeout_defaults(self):
        self.assertEqual(events.timeout_default("PreToolUse"), 600)
        self.assertEqual(events.timeout_default("UserPromptSubmit"), 30)
        self.assertEqual(events.timeout_default("PreModelSwitch"), 30)
        self.assertEqual(events.timeout_default("PostModelSwitch"), 30)
        self.assertEqual(events.timeout_default("MessageDisplay"), 10)
        self.assertEqual(events.timeout_default("SessionEnd"), 1.5)

    def test_can_block(self):
        for event in ("PreToolUse", "UserPromptSubmit", "Stop",
                      "PreModelSwitch", "WorktreeCreate"):
            self.assertTrue(events.can_block(event), event)
        for event in ("PostToolUse", "SessionEnd", "Notification",
                      "SubagentStop"):
            self.assertFalse(events.can_block(event), event)

    def test_matcher_fields(self):
        self.assertIsNone(events.matcher_field("Stop"))
        self.assertIsNone(events.matcher_field("CwdChanged"))
        self.assertEqual(events.matcher_field("PreToolUse"), "tool name")
        self.assertEqual(events.matcher_field("SessionEnd"),
                         "why the session ended")

    def test_matcher_domains(self):
        self.assertIn("startup", events.matcher_domain("SessionStart"))
        self.assertIn("rate_limit", events.matcher_domain("StopFailure"))
        self.assertIsNone(events.matcher_domain("PreToolUse"))


if __name__ == "__main__":
    unittest.main()
