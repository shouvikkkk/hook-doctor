"""Tests for payload synthesis (hookdoctor.payloads)."""

import unittest

from hookdoctor import config, payloads


def _hook(event, matcher="", handler=None):
    return config.Hook(event, matcher, handler or {"type": "command",
                                                   "command": "x.sh"},
                       "/tmp/settings.json", "settings")


class TestPayloads(unittest.TestCase):
    def test_common_fields(self):
        payload = payloads.build_payload("Notification", _hook("Notification"),
                                         "/tmp/proj", "/tmp/t.jsonl")
        for field in ("session_id", "transcript_path", "cwd",
                      "hook_event_name", "permission_mode"):
            self.assertIn(field, payload)
        self.assertEqual(payload["hook_event_name"], "Notification")
        self.assertEqual(payload["cwd"], "/tmp/proj")

    def test_pretooluse_benign_and_hostile(self):
        hook = _hook("PreToolUse", "Bash",
                     {"type": "command", "command": "guard.sh",
                      "if": "Bash(rm *)"})
        benign = payloads.build_payload("PreToolUse", hook, "/tmp",
                                        "/tmp/t.jsonl", hostile=False)
        hostile = payloads.build_payload("PreToolUse", hook, "/tmp",
                                         "/tmp/t.jsonl", hostile=True)
        self.assertEqual(benign["tool_name"], "Bash")
        self.assertEqual(benign["tool_input"]["command"], "git status")
        self.assertIn("rm -rf", hostile["tool_input"]["command"])
        self.assertIn("tool_use_id", benign)

    def test_pretooluse_edit_probe(self):
        hook = _hook("PreToolUse", "Edit",
                     {"type": "command", "command": "guard.sh",
                      "if": "Edit(*.env)"})
        hostile = payloads.build_payload("PreToolUse", hook, "/tmp",
                                         "/tmp/t.jsonl", hostile=True)
        self.assertEqual(hostile["tool_name"], "Edit")
        self.assertTrue(hostile["tool_input"]["file_path"].endswith(".env"))

    def test_stop_payload(self):
        payload = payloads.build_payload("Stop", _hook("Stop"), "/tmp",
                                         "/tmp/t.jsonl")
        self.assertIn("stop_hook_active", payload)
        self.assertFalse(payload["stop_hook_active"])
        self.assertIn("last_assistant_message", payload)

    def test_stop_payload_active(self):
        payload = payloads.stop_payload("/tmp", "/tmp/t.jsonl", True)
        self.assertTrue(payload["stop_hook_active"])

    def test_session_end_reason(self):
        payload = payloads.build_payload("SessionEnd", _hook("SessionEnd"),
                                         "/tmp", "/tmp/t.jsonl")
        self.assertEqual(payload["reason"], "clear")

    def test_all_events_produce_payloads(self):
        from hookdoctor import events
        for event in events.EVENTS:
            payload = payloads.build_payload(event, _hook(event), "/tmp",
                                             "/tmp/t.jsonl")
            self.assertEqual(payload["hook_event_name"], event, event)


if __name__ == "__main__":
    unittest.main()
