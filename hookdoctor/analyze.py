"""Classify a hook run the way Claude Code would -- and judge it.

The exit-code / stdout matrix comes straight from the docs ("Exit code
output"):
- exit 0 + valid JSON object: success, the fields apply.
- exit 0 + invalid JSON: non-blocking error (the action goes ahead).
- exit 0 + plain text: success.
- exit 2: blocking error on events that can block; stderr is the reason.
  Valid JSON is still read but cannot override the block.
- any other exit code + valid JSON: the fields alone decide (unless the
  hook is one whose failure blocks the action).
- any other exit code + plain text/empty: non-blocking error.
- WorktreeCreate/WorktreeRemove: any non-zero exit fails the operation.
- Stop/SubagentStop/TaskCompleted and plugin UserPromptSubmit: exit 2
  with empty stdout and a "file missing" stderr is treated as a
  non-blocking error (the hook never ran).

On top of the matrix, hook-doctor judges the *guard* question: a hook
on a blocking event that never blocks anything is either observational
(fine) or a dead guard (the failure this tool exists to catch). The
distinction is heuristic and always stated in the finding: a hook is
"guard-shaped" when its script or command text mentions blocking
(deny/block/exit 2/permissionDecision) or its name says so
(block/guard/deny/protect/forbid), or when its ``if`` filter narrows it
to a dangerous tool call. An always-allow run of a guard-shaped hook is
a failure; of an observational hook, a pass with an informational note.
"""

import re

from . import events as events_mod

_GUARD_WORDS = ("deny", "block", "forbid", "protect", "guard", "refuse",
                "permissiondecision", "exit 2")

# Events where Claude Code treats "exit 2, empty stdout, missing-file
# stderr" as a non-blocking error instead of a block.
_MISSING_FILE_TOLERANT = ("Stop", "SubagentStop", "TaskCompleted")


def looks_guard_shaped(hook, script_text=""):
    """Heuristic: is this hook meant to stop something?

    Returns (is_guard, reasons). Always advisory -- the finding that
    uses it names the reasons, so a wrong guess is visible.
    """
    reasons = []
    hay = " ".join([
        hook.event, hook.matcher or "",
        str(hook.handler.get("command", "") or ""),
        str(hook.handler.get("if", "") or ""),
        script_text or "",
    ]).lower()
    for word in _GUARD_WORDS:
        # Word-boundary match: "blocks" in a comment ("never blocks
        # anything") must not make an observational hook look like a
        # guard, while "block-rm.sh" and "exit 2" still count.
        if re.search(r"\b" + re.escape(word) + r"\b", hay):
            reasons.append("mentions %r" % word)
    if hook.handler.get("onFailure") == "block":
        reasons.append("sets onFailure: block")
    if hook.event in ("PreToolUse", "UserPromptSubmit", "PreModelSwitch") \
            and hook.handler.get("if"):
        reasons.append("has an `if` filter narrowing a blocking event")
    return bool(reasons), reasons


def classify_run(event, result):
    """Classify one run's outcome from Claude Code's point of view.

    Returns a dict: {outcome, decision, detail}. ``outcome`` is one of
    "allow", "block", "non-blocking-error", "timeout", "spawn-error",
    "fail". ``decision`` is the parsed JSON decision (or None).
    """
    if result.spawn_error:
        return {"outcome": "spawn-error", "decision": None,
                "detail": result.spawn_error}
    if result.timed_out:
        return {"outcome": "timeout", "decision": None,
                "detail": "hook exceeded the test timeout of %.1fs"
                % result.duration_s}
    exit_code = result.exit_code
    parsed = result.stdout_json
    json_error = result.stdout_json_error
    if event in ("WorktreeCreate", "WorktreeRemove"):
        if exit_code != 0:
            return {"outcome": "fail", "decision": parsed,
                    "detail": "any non-zero exit fails %s" % event}
        return {"outcome": "allow", "decision": parsed,
                "detail": "exit 0"}
    if exit_code == 2:
        if event in _MISSING_FILE_TOLERANT \
                and result.stdout.strip() == "" \
                and "no such file" in result.stderr.lower():
            return {"outcome": "non-blocking-error", "decision": None,
                    "detail": "exit 2 with missing-file stderr is treated "
                    "as a non-blocking error on %s" % event}
        if not events_mod.can_block(event):
            return {"outcome": "block-ignored", "decision": parsed,
                    "detail": "exit 2 cannot block on %s; Claude Code "
                    "ignores the block" % event}
        return {"outcome": "block", "decision": parsed,
                "detail": "exit 2 blocks the action"}
    if exit_code == 0:
        if parsed is not None:
            return {"outcome": "allow", "decision": parsed,
                    "detail": "exit 0 with valid JSON output"}
        text = result.stdout.strip()
        if text == "":
            return {"outcome": "allow", "decision": None,
                    "detail": "exit 0, no JSON decision"}
        if text.startswith("{") and text.endswith("}"):
            # Looks like JSON but is not: a parse failure, hence a
            # non-blocking error. Plain text, by contrast, is success.
            return {"outcome": "non-blocking-error", "decision": None,
                    "detail": "exit 0 but stdout is not usable JSON: %s"
                    % json_error}
        return {"outcome": "allow", "decision": None,
                "detail": "exit 0 with plain-text stdout"}
    # Any other exit code.
    if parsed is not None:
        return {"outcome": "allow", "decision": parsed,
                "detail": "exit %d but valid JSON decides the outcome"
                % exit_code}
    return {"outcome": "non-blocking-error", "decision": None,
            "detail": "exit %d with no usable JSON: the action goes ahead"
            % exit_code}


def decision_allows(decision):
    """Does a parsed JSON decision let the action through?"""
    if not isinstance(decision, dict):
        return None
    specific = decision.get("hookSpecificOutput") or {}
    perm = specific.get("permissionDecision")
    if perm in ("allow", "ask", "defer"):
        return True
    if perm == "deny":
        return False
    if decision.get("decision") == "block":
        return False
    if decision.get("continue") is False:
        return False
    return None


def check_decision_placement(event, decision):
    """Flag decision fields Claude Code ignores because of placement.

    The docs: on PreToolUse the decision goes inside
    ``hookSpecificOutput``; a top-level ``permissionDecision`` is ignored
    without an error. Returns a list of finding dicts.
    """
    findings = []
    if not isinstance(decision, dict):
        return findings
    if "permissionDecision" in decision and event == "PreToolUse":
        findings.append({
            "code": "decision-misplaced", "severity": "error",
            "message": "`permissionDecision` at the top level is ignored "
            "by Claude Code on PreToolUse; it belongs inside "
            "`hookSpecificOutput`"})
    if "hookSpecificOutput" in decision:
        specific = decision["hookSpecificOutput"]
        if not isinstance(specific, dict):
            findings.append({
                "code": "decision-malformed", "severity": "error",
                "message": "`hookSpecificOutput` must be an object"})
        elif specific.get("hookEventName") not in (None, event):
            findings.append({
                "code": "decision-wrong-event", "severity": "warning",
                "message": "`hookSpecificOutput.hookEventName` is %r but "
                "the event is %r" % (specific.get("hookEventName"), event)})
    return findings


def fail_open_assessment(hook, run_outcome):
    """Is this hook fail-open, and does that matter?

    A hook on a blocking event fails open when a crash, a timeout, or a
    failure to start lets the action through. ``onFailure: "block"``
    (v2.1.295+) is the opt-in fail-closed. Returns findings.
    """
    findings = []
    if not events_mod.can_block(hook.event):
        return findings
    if hook.handler.get("onFailure") == "block":
        return findings
    if hook.handler.get("async"):
        return findings  # async hooks cannot block at all; warned elsewhere
    if run_outcome in ("spawn-error", "timeout", "non-blocking-error"):
        findings.append({
            "code": "fails-open", "severity": "error",
            "message": "this %s hook %s and the action goes ahead anyway "
            "(no `onFailure: \"block\"`); a guard that fails this way is "
            "worse than no guard, because the session believes it is "
            "protected" % (hook.event, run_outcome)})
    else:
        findings.append({
            "code": "fails-open-risk", "severity": "warning",
            "message": "no `onFailure: \"block\"` set: if this hook ever "
            "crashes or times out, the action goes ahead (Claude Code "
            "v2.1.295+ supports onFailure: block)"})
    return findings


def slow_hook_assessment(hook, duration_s):
    """Flag hooks slow enough to hurt the session or miss their budget."""
    findings = []
    event = hook.event
    if event == "SessionEnd":
        budget = hook.handler.get("timeout") or 1.5
        if duration_s > budget:
            findings.append({
                "code": "sessionend-over-budget", "severity": "error",
                "message": "SessionEnd hooks share a %.1fs budget; this "
                "one took %.1fs and would be cancelled in a real session"
                % (budget, duration_s)})
        elif duration_s > 1.0:
            findings.append({
                "code": "sessionend-slow", "severity": "warning",
                "message": "SessionEnd hook took %.1fs of a %.1fs budget"
                % (duration_s, budget)})
        return findings
    if event in ("PreToolUse", "PostToolUse", "UserPromptSubmit",
                 "Stop", "SubagentStop"):
        if duration_s > 5.0:
            findings.append({
                "code": "slow-hook", "severity": "warning",
                "message": "%s hook took %.1fs; it runs on every matching "
                "event, so this taxes the whole session" % (event, duration_s)})
        elif duration_s > 1.0:
            findings.append({
                "code": "slow-hook", "severity": "info",
                "message": "%s hook took %.1fs" % (event, duration_s)})
    return findings


def stderr_swallowed_assessment(result):
    """stderr on exit 0 is never seen by Claude -- flag it."""
    if result.exit_code == 0 and result.stderr.strip():
        return [{
            "code": "stderr-swallowed", "severity": "warning",
            "message": "hook printed to stderr but exited 0, so Claude "
            "Code never shows that text to the model; warnings written "
            "here are silently lost"}]
    return []


def stop_loop_assessment(outcomes):
    """Judge a Stop-hook livelock simulation.

    ``outcomes`` is a list of per-iteration outcome dicts from
    classify_run, fed with stop_hook_active=False then True. A hook
    that keeps blocking after stop_hook_active is True ignores the flag
    Claude Code sets to tell it the session is already continuing
    because of a stop hook -- it will loop until the 8-block cap.
    """
    if not outcomes:
        return []
    blocked = [o["outcome"] == "block" for o in outcomes]
    if blocked and all(blocked):
        return [{
            "code": "stop-loop", "severity": "error",
            "message": "Stop hook blocks on every iteration including "
            "with stop_hook_active=true: it ignores the flag and would "
            "loop until Claude Code's 8-consecutive-block cap"}]
    if blocked and blocked[0] and not blocked[-1]:
        return [{
            "code": "stop-loop-ok", "severity": "info",
            "message": "Stop hook blocks once, then allows when "
            "stop_hook_active=true: it respects the continuation flag"}]
    return []
