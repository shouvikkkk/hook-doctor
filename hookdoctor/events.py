"""The 33 Claude Code hook events, as documented in the official hooks
reference (code.claude.com/docs/en/hooks, read 2026-11-11).

Each entry records what hook-doctor needs to synthesise a faithful test
payload and to judge the outcome:

- ``can_block``: whether exit code 2 (or a JSON decision) can stop the
  action that fired the event. On events that cannot block, a hook that
  exits 2 is at best noise and at worst a misunderstanding.
- ``matcher_field``: what the ``matcher`` filters on for this event, or
  None when the event has no matcher support (the hook always fires).
- ``timeout_default``: the documented default timeout in seconds for a
  ``command`` hook on this event (600 unless the docs lower it).
- ``payload``: event-specific input fields, with realistic values.
  Fields marked "verified" come from the docs page; a few shapes (notably
  ``tool_response``) are best-effort and marked as such in the code.

Timeout facts from the docs:
- command/http/mcp_tool default 600s; prompt 30s; agent 60s.
- Lowered to 30s on UserPromptSubmit, PreModelSwitch, PostModelSwitch;
  10s on MessageDisplay.
- SessionEnd hooks share a 1.5s budget; a per-hook ``timeout`` raises it
  (up to 60s).
- A timed-out hook's output is discarded on most events; on PreToolUse
  the tool call goes ahead (unless onFailure is "block"); on
  PreModelSwitch a timeout blocks the switch.
"""

# The documented per-event default timeout (seconds) for a command hook,
# where it differs from the 600s general default.
_TIMEOUT_OVERRIDE = {
    "UserPromptSubmit": 30,
    "PreModelSwitch": 30,
    "PostModelSwitch": 30,
    "MessageDisplay": 10,
    "SessionEnd": 1.5,
}

# Events on which exit code 2 can block the action. Sourced from the
# docs' per-event sections and the "Exit code 2 behavior per event" table.
_BLOCKING_EVENTS = {
    "PreToolUse",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "Stop",
    "PreModelSwitch",
    "PermissionRequest",
    "WorktreeCreate",
    "WorktreeRemove",
}

# Events with no matcher support: the hook always fires.
_NO_MATCHER = {
    "UserPromptSubmit",
    "PostToolBatch",
    "Stop",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "WorktreeCreate",
    "WorktreeRemove",
    "MessageDisplay",
    "CwdChanged",
}

# What the matcher filters on, per event (None = no matcher support).
_MATCHER_FIELD = {
    "PreToolUse": "tool name",
    "PostToolUse": "tool name",
    "PostToolUseFailure": "tool name",
    "PermissionRequest": "tool name",
    "PermissionDenied": "tool name",
    "SessionStart": "how the session started",
    "Setup": "which CLI flag triggered setup",
    "SessionEnd": "why the session ended",
    "Notification": "notification type",
    "SubagentStart": "agent type",
    "SubagentStop": "agent type",
    "PreCompact": "what triggered compaction",
    "PostCompact": "what triggered compaction",
    "PreModelSwitch": "model the session switches to",
    "PostModelSwitch": "model the session switches to",
    "ConfigChange": "configuration source",
    "DirectoryAdded": "how the directory was added",
    "FileChanged": "literal filenames to watch",
    "StopFailure": "error type",
    "InstructionsLoaded": "load reason",
    "UserPromptExpansion": "command name",
    "Elicitation": "MCP server name",
    "ElicitationResult": "MCP server name",
}

# Canonical matcher values for events whose matcher domain is a closed
# set in the docs. Used to spot matchers that can never match anything.
_MATCHER_DOMAIN = {
    "SessionStart": {"startup", "resume", "clear", "compact", "fork"},
    "Setup": {"init", "maintenance"},
    "SessionEnd": {"clear", "resume", "logout", "prompt_input_exit", "other"},
    "PreCompact": {"manual", "auto"},
    "PostCompact": {"manual", "auto"},
    "Notification": {
        "permission_prompt", "idle_prompt", "auth_success",
        "auth_storage_failure", "elicitation_dialog",
        "elicitation_url_dialog", "elicitation_complete",
        "elicitation_response", "agent_needs_input", "agent_completed",
        "quota_auto_resume_fired", "quota_auto_resume_stale",
        "quota_auto_resume_disabled", "plugin_notification",
    },
    "StopFailure": {
        "rate_limit", "overloaded", "authentication_failed",
        "oauth_org_not_allowed", "account_on_hold", "billing_error",
        "invalid_request", "model_not_found", "server_error",
        "max_output_tokens", "cloud_credential_error", "unknown",
    },
    "InstructionsLoaded": {
        "session_start", "nested_traversal", "path_glob_match",
        "include", "compact",
    },
    "ConfigChange": {
        "user_settings", "project_settings", "local_settings",
        "policy_settings", "skills",
    },
    "DirectoryAdded": {"slash_command", "register_repo_root"},
}

#: All 33 documented hook events.
EVENTS = (
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion",
    "PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse",
    "PostToolUseFailure", "PostToolBatch", "Notification", "MessageDisplay",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted",
    "Stop", "StopFailure", "TeammateIdle", "InstructionsLoaded",
    "ConfigChange", "CwdChanged", "DirectoryAdded", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch", "Elicitation", "ElicitationResult",
    "SessionEnd",
)


def timeout_default(event):
    """Documented default timeout (seconds) for a command hook on event."""
    return _TIMEOUT_OVERRIDE.get(event, 600)


def can_block(event):
    """Whether exit code 2 can stop the action on this event."""
    return event in _BLOCKING_EVENTS


def matcher_field(event):
    """What the matcher filters on, or None if the event has no matcher."""
    if event in _NO_MATCHER:
        return None
    return _MATCHER_FIELD.get(event)


def matcher_domain(event):
    """Closed set of matcher values, or None if the domain is open."""
    return _MATCHER_DOMAIN.get(event)
