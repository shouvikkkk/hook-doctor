"""Synthesise the JSON input a hook would receive from Claude Code.

Common fields (docs, "Common input fields"): ``session_id``,
``transcript_path``, ``cwd``, ``hook_event_name``; ``prompt_id`` when a
prompt is being processed; ``permission_mode`` on most events;
``agent_id``/``agent_type`` inside subagents. Event-specific fields are
taken from the docs' per-event sections; where a field's exact shape was
not in the text hook-doctor read (notably ``tool_response``), the value
is a best-effort placeholder and the fact is noted in the report.

Probe payloads for guard testing: for tool events hook-doctor runs two
probes -- a benign one and a hostile one -- so a guard that never
blocks is distinguishable from one that blocks correctly. The hostile
probe is tailored to the hook's ``if``/matcher when it names a tool:
``Bash`` gets a destructive command, file tools get a ``.env`` path.
"""

import os
import uuid

SESSION_ID = "hook-doctor-test-session"
PROMPT_ID = "00000000-0000-4000-8000-000000000000"

# A destructive command used as the hostile probe for Bash guards.
HOSTILE_BASH = "rm -rf /tmp/hook-doctor-probe"
BENIGN_BASH = "git status"
HOSTILE_FILE = "/tmp/hook-doctor-probe/.env"
BENIGN_FILE = "/tmp/hook-doctor-probe/README.md"


def _common(event, cwd, transcript_path):
    return {
        "session_id": SESSION_ID,
        "prompt_id": PROMPT_ID,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "hook_event_name": event,
        "permission_mode": "default",
    }


def _tool_input_for(probe, tool):
    """A realistic tool_input for the probe's tool."""
    if tool in ("Bash",):
        return {"command": probe["command"], "description": probe.get("description", "")}
    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return {"file_path": probe["file_path"]}
    if tool in ("Glob", "Grep"):
        return {"pattern": probe.get("pattern", "*.py")}
    return {"command": probe.get("command", "")}


def _tool_name_for(hook, probe):
    """Which tool the probe pretends to call, from matcher/`if`."""
    text = (hook.matcher or "") + " " + str(hook.handler.get("if", "") or "")
    for tool in ("Bash", "Edit", "Write", "Read", "MultiEdit", "Glob", "Grep"):
        if tool in text:
            return tool
    return "Bash"


def _probe_for(hook, hostile):
    tool = _tool_name_for(hook, hostile)
    if tool == "Bash":
        return {"tool": "Bash",
                "command": HOSTILE_BASH if hostile else BENIGN_BASH}
    return {"tool": tool,
            "file_path": HOSTILE_FILE if hostile else BENIGN_FILE,
            "command": HOSTILE_BASH if hostile else BENIGN_BASH}


def build_payload(event, hook, cwd, transcript_path, hostile=False):
    """Build one synthetic hook-input payload.

    ``hostile`` selects the hostile probe for tool events; other events
    ignore it. Returns a JSON-serialisable dict.
    """
    payload = _common(event, cwd, transcript_path)
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "PermissionRequest", "PermissionDenied"):
        probe = _probe_for(hook, hostile)
        payload["tool_name"] = probe["tool"]
        payload["tool_input"] = _tool_input_for(probe, probe["tool"])
        payload["tool_use_id"] = "toolu_hook_doctor_01"
        if event == "PostToolUse":
            # Best-effort shape: the docs name the field but hook-doctor
            # did not verify the exact tool_response schema.
            payload["tool_response"] = {
                "stdout": "ok\n", "stderr": "", "exitCode": 0,
                "interrupted": False,
            }
        if event == "PermissionDenied":
            payload["reason"] = "denied by auto mode"
        if event == "PostToolUseFailure":
            payload["tool_response"] = {
                "stdout": "", "stderr": "boom\n", "exitCode": 1,
                "interrupted": False,
            }
    elif event == "PostToolBatch":
        payload["tool_calls"] = [
            {"tool_name": "Bash",
             "tool_input": {"command": BENIGN_BASH, "description": ""},
             "tool_use_id": "toolu_hook_doctor_01"},
            {"tool_name": "Read",
             "tool_input": {"file_path": BENIGN_FILE},
             "tool_use_id": "toolu_hook_doctor_02"},
        ]
    elif event == "SessionStart":
        payload["source"] = "startup"
        payload["model"] = "claude-opus-5"
    elif event == "Setup":
        payload["trigger"] = "init"
    elif event == "UserPromptSubmit":
        payload["prompt"] = "Fix the failing test in tests/test_cli.py"
    elif event == "UserPromptExpansion":
        payload["expansion_type"] = "slash_command"
        payload["command_name"] = "test"
        payload["command_args"] = ""
        payload["command_source"] = "user"
        payload["prompt"] = "/test"
    elif event == "MessageDisplay":
        payload["turn_id"] = "turn_hook_doctor_01"
        payload["message_id"] = "msg_hook_doctor_01"
        payload["index"] = 0
        payload["final"] = True
        payload["delta"] = "Here is the fix."
    elif event == "Notification":
        payload["message"] = "Permission prompt waiting"
        payload["notification_type"] = "permission_prompt"
    elif event == "SubagentStart":
        payload["agent_id"] = "agent_hook_doctor_01"
        payload["agent_type"] = "Explore"
    elif event == "SubagentStop":
        payload["stop_hook_active"] = False
        payload["agent_id"] = "agent_hook_doctor_01"
        payload["agent_type"] = "Explore"
        payload["agent_transcript_path"] = transcript_path
        payload["last_assistant_message"] = "Exploration done."
    elif event in ("TaskCreated", "TaskCompleted"):
        payload["task_id"] = "task_hook_doctor_01"
        payload["task_subject"] = "Write the failing test"
    elif event == "Stop":
        payload["stop_hook_active"] = False
        payload["last_assistant_message"] = "All tests pass."
        payload["background_tasks"] = []
        payload["session_crons"] = []
    elif event == "StopFailure":
        payload["error"] = "rate_limit"
        payload["last_assistant_message"] = "Hit the rate limit."
    elif event == "TeammateIdle":
        payload["teammate_name"] = "reviewer"
        payload["team_name"] = "default"
    elif event == "InstructionsLoaded":
        payload["reason"] = "session_start"
    elif event == "ConfigChange":
        payload["source"] = "project_settings"
        payload["file_path"] = os.path.join(cwd, ".claude", "settings.json")
    elif event == "CwdChanged":
        payload["old_cwd"] = cwd
        payload["new_cwd"] = os.path.join(cwd, "subdir")
    elif event == "DirectoryAdded":
        payload["directory"] = os.path.join(cwd, "other")
        payload["source"] = "slash_command"
    elif event == "FileChanged":
        payload["file_path"] = os.path.join(cwd, ".env")
        payload["event"] = "modified"
    elif event == "WorktreeCreate":
        payload["name"] = "bold-oak-a3f2"
    elif event == "WorktreeRemove":
        payload["worktree_path"] = os.path.join(cwd, "..", "bold-oak-a3f2")
    elif event == "PreCompact":
        payload["trigger"] = "manual"
        payload["custom_instructions"] = None
    elif event == "PostCompact":
        payload["trigger"] = "manual"
        payload["compact_summary"] = "Session summary."
    elif event in ("PreModelSwitch", "PostModelSwitch"):
        payload["from_model"] = "claude-sonnet-5"
        payload["to_model"] = "claude-opus-5"
    elif event == "Elicitation":
        payload["mcp_server_name"] = "example-server"
        payload["message"] = "Approve access?"
        payload["mode"] = "form"
    elif event == "ElicitationResult":
        payload["mcp_server_name"] = "example-server"
        payload["action"] = "accept"
    elif event == "SessionEnd":
        payload["reason"] = "clear"
    return payload


def stop_payload(cwd, transcript_path, stop_hook_active):
    """A Stop payload with an explicit stop_hook_active value."""
    payload = build_payload("Stop", None, cwd, transcript_path)
    payload["stop_hook_active"] = stop_hook_active
    return payload


def unique_session_id():
    return "hook-doctor-%s" % uuid.uuid4().hex[:12]
