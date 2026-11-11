"""hook-doctor CLI: check a Claude Code hook configuration.

Usage:
    hook-doctor check [path] [--settings FILE] [--no-user]
                      [--no-run] [--test-timeout SECONDS]
                      [--format text|json]

What it does, per hook:
1. Static validation -- unknown event/type, missing required fields,
   ``if`` on a non-tool event (never runs), ``once`` outside skill
   frontmatter (ignored), ``async`` on a blocking event (cannot block),
   bad timeouts, ``onFailure`` on a type that ignores it.
2. Matcher analysis -- exact-list matchers checked against the event's
   documented domain; regex matchers compiled; a matcher that can never
   match anything is an error. An ``if`` naming a different tool than
   the matcher is a contradiction: the hook can never run.
3. Command resolution -- for command hooks, the executable or script
   must exist and be runnable; a missing script is a dead guard, and
   (per the docs) a hook that cannot start is a non-blocking error, so
   the action goes ahead unguarded.
4. Live probes (unless --no-run) -- each command hook is run in a
   sandbox temp dir with a scrubbed environment and the documented JSON
   payload for its event. Tool-event hooks get a benign and a hostile
   probe; Stop hooks get a livelock simulation (up to 8 iterations,
   the documented block cap). Outcomes are classified with the docs'
   exit-code / stdout matrix, and fail-open risk, slowness, swallowed
   stderr and misplaced JSON decisions are reported.

Exit codes: 0 all hooks healthy, 1 warnings only, 2 a hook fails,
3 usage error (no hooks found, unreadable target).
"""

import argparse
import json
import os
import re
import sys

from . import __version__, analyze, config, events as events_mod
from . import matchers, payloads, report, runner


def _read_script_text(hook, project_dir):
    """Read the script a command hook runs, if it is a local file.

    The script may be the command itself (``.claude/hooks/guard.sh``)
    or an argument to an interpreter (``sh .claude/hooks/guard.sh``,
    the form the examples use so they work without an exec bit). The
    first token that resolves to a readable file wins.
    """
    handler = hook.handler
    if handler.get("type") != "command":
        return ""
    command = str(handler.get("command") or "")
    candidates = []
    if handler.get("args") is not None:
        candidates.append(command)
    else:
        try:
            import shlex
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = command.split()
        candidates.extend(tokens)
    for path in candidates:
        for placeholder in ("${CLAUDE_PROJECT_DIR}",
                            "${CLAUDE_PLUGIN_ROOT}",
                            "${CLAUDE_PLUGIN_DATA}"):
            path = path.replace(placeholder, project_dir)
        if os.path.isabs(path):
            full = path
        else:
            full = os.path.join(project_dir, path)
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8",
                          errors="replace") as fh:
                    return fh.read(4096)
            except OSError:
                return ""
    return ""


def _matcher_findings(hook):
    """Static matcher analysis."""
    findings = []
    event = hook.event
    if event not in events_mod.EVENTS:
        return findings
    matcher = hook.matcher or ""
    if events_mod.matcher_field(event) is None and matcher not in ("", "*"):
        findings.append({"code": "matcher-ignored", "severity": "warning",
                         "message": "%s has no matcher support; this "
                         "matcher is ignored and the hook fires on every "
                         "occurrence" % event})
        return findings
    kind = matchers.matcher_kind(matcher, event)
    if kind == "regex":
        try:
            re.compile(matcher)
        except re.error as exc:
            findings.append({"code": "bad-matcher", "severity": "error",
                             "message": "matcher is not a valid regular "
                             "expression: %s" % exc})
    domain = events_mod.matcher_domain(event)
    if domain is not None and kind == "exact":
        values = matchers.split_exact_list(matcher)
        bad = [v for v in values if v not in domain]
        if values and len(bad) == len(values):
            findings.append({"code": "matcher-never-matches",
                             "severity": "error",
                             "message": "matcher %r matches nothing: %s "
                             "only accepts %s"
                             % (matcher, event, sorted(domain))})
        elif bad:
            findings.append({"code": "matcher-partly-invalid",
                             "severity": "warning",
                             "message": "matcher values %s never occur on "
                             "%s (domain: %s)"
                             % (bad, event, sorted(domain))})
    return findings


def _if_matcher_contradiction(hook):
    """An `if` naming a different tool than the matcher can never run."""
    findings = []
    if not hook.handler.get("if"):
        return findings
    if hook.event not in ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                          "PermissionRequest", "PermissionDenied"):
        return findings
    parsed = matchers._parse_if_rule(str(hook.handler["if"]))
    if parsed is None:
        return [{"code": "bad-if", "severity": "error",
                 "message": "malformed `if` rule: %r" % hook.handler["if"]}]
    if_tool = parsed[0]
    kind = matchers.matcher_kind(hook.matcher or "", hook.event)
    if kind == "exact":
        tools = matchers.split_exact_list(hook.matcher or "")
        if tools and if_tool not in tools:
            findings.append({
                "code": "if-matcher-contradiction", "severity": "error",
                "message": "matcher only fires for %s but `if` only "
                "matches %s: this hook can never run"
                % (tools, if_tool)})
    return findings


def _command_resolution_findings(hook, project_dir):
    """Check the hook's command exists and is runnable."""
    findings = []
    handler = hook.handler
    if handler.get("type") != "command":
        return findings
    command = str(handler.get("command") or "")
    if handler.get("args") is not None:
        path = command
        for placeholder in ("${CLAUDE_PROJECT_DIR}",
                            "${CLAUDE_PLUGIN_ROOT}",
                            "${CLAUDE_PLUGIN_DATA}"):
            path = path.replace(placeholder, project_dir)
        if os.path.isabs(path):
            candidate = path
        else:
            candidate = None
            for directory in os.environ.get("PATH", "").split(os.pathsep):
                trial = os.path.join(directory, path)
                if os.path.isfile(trial) and os.access(trial, os.X_OK):
                    candidate = trial
                    break
        if candidate is None:
            findings.append({"code": "command-not-found", "severity": "error",
                             "message": "exec-form command %r not found on "
                             "PATH" % path})
        return findings
    kind, detail = runner.resolve_command_path(command, project_dir)
    if kind == "missing":
        findings.append({"code": "command-not-found", "severity": "error",
                         "message": "hook command %r not found: no such "
                         "file and nothing on PATH; Claude Code reports a "
                         "hook error and the action goes ahead" % detail})
    elif kind == "file-not-executable":
        findings.append({"code": "command-not-executable", "severity": "error",
                         "message": "hook script %s is not executable; "
                         "Claude Code cannot run it" % detail})
    # A script passed to an interpreter (``sh .claude/hooks/guard.sh``)
    # must exist too. This matters because the wrapper hides the miss:
    # dash exits 2 when it cannot open a script, so a missing script
    # would silently *block* every matching call instead of erroring.
    import shlex
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    interpreters = {"sh", "bash", "python", "python3", "node", "ruby",
                    "perl", "pwsh", "powershell", "dash"}
    for i, token in enumerate(tokens):
        if i > 0 and tokens[i - 1] in interpreters:
            path = token
            for placeholder in ("${CLAUDE_PROJECT_DIR}",
                                "${CLAUDE_PLUGIN_ROOT}",
                                "${CLAUDE_PLUGIN_DATA}"):
                path = path.replace(placeholder, project_dir)
            full = path if os.path.isabs(path) \
                else os.path.join(project_dir, path)
            if not os.path.isfile(full):
                findings.append({
                    "code": "script-not-found", "severity": "error",
                    "message": "hook script %r does not exist; the "
                    "interpreter cannot open it (dash exits 2 on a "
                    "missing script, so this would block every matching "
                    "call instead of guarding it)" % token})
            break
    return findings


def _jq_findings(hook, script_text):
    """Hooks that parse JSON with jq degrade to no-ops without it."""
    if "jq" not in script_text:
        return []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isfile(os.path.join(directory, "jq")):
            return []
    return [{"code": "jq-missing", "severity": "warning",
             "message": "hook script uses `jq` but jq is not on PATH; "
             "without it the hook fails on every run (and, on a blocking "
             "event, the action goes ahead)"}]


def _effective_timeout(hook, test_timeout):
    """Timeout for a test run: the hook's own timeout (or the event's
    documented default) capped by --test-timeout."""
    configured = hook.handler.get("timeout")
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        base = float(configured)
    else:
        base = events_mod.timeout_default(hook.event)
    return min(base, test_timeout)


def _test_handler(hook, project_dir):
    """Rewrite a handler's command so the test run finds its script.

    hook-doctor executes hooks in a sandbox temp dir, never the real
    project, so a relative script path (``.claude/hooks/guard.sh``) or a
    ``${CLAUDE_PROJECT_DIR}`` placeholder would not resolve there.
    This rewrites the entry script -- and only the entry script -- to
    its absolute path under the project dir. Anything the script itself
    references relatively is its own business and is left alone.
    """
    handler = dict(hook.handler)
    if handler.get("type") != "command":
        return handler
    command = str(handler.get("command") or "")
    for placeholder in ("${CLAUDE_PROJECT_DIR}", "${CLAUDE_PLUGIN_ROOT}",
                        "${CLAUDE_PLUGIN_DATA}"):
        command = command.replace(placeholder, project_dir)
    if handler.get("args") is not None:
        if not os.path.isabs(command):
            candidate = os.path.join(project_dir, command)
            if os.path.isfile(candidate):
                command = candidate
        handler["command"] = command
        handler["args"] = [
            str(a).replace("${CLAUDE_PROJECT_DIR}", project_dir)
            .replace("${CLAUDE_PLUGIN_ROOT}", project_dir)
            .replace("${CLAUDE_PLUGIN_DATA}", project_dir)
            for a in handler["args"]
        ]
        return handler
    import shlex
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return handler
    for token in tokens:
        if token.startswith("-") or "/" not in token:
            continue
        if os.path.isabs(token):
            continue
        candidate = os.path.join(project_dir, token)
        if os.path.isfile(candidate):
            handler["command"] = command.replace(token, candidate, 1)
            break
    return handler


def _probe_hook(hook, project_dir, transcript_path, test_timeout):
    """Run a command hook's probes; returns (runtime, findings).

    ``runtime`` summarises the runs for the report; ``findings`` holds
    the judged results. Stop hooks get the livelock simulation;
    tool-event hooks get benign + hostile probes; everything else gets
    a single payload.
    """
    findings = []
    timeout = _effective_timeout(hook, test_timeout)
    script_text = _read_script_text(hook, project_dir)
    is_guard, guard_reasons = analyze.looks_guard_shaped(hook, script_text)
    event = hook.event
    runs = []  # (probe_kind, payload, result, classification)
    test_handler = _test_handler(hook, project_dir)

    def do_run(payload):
        result = runner.run_command_hook(test_handler, payload, timeout,
                                         workdir=None)
        return result, analyze.classify_run(event, result)

    if event == "Stop":
        # Livelock simulation: keep feeding Stop payloads while the hook
        # blocks, flipping stop_hook_active to True after the first
        # block, up to the documented 8-block cap.
        outcomes = []
        worst_duration = 0.0
        exit_codes = []
        for i in range(8):
            payload = payloads.stop_payload(
                project_dir, transcript_path, stop_hook_active=(i > 0))
            result, classification = do_run(payload)
            outcomes.append(classification)
            worst_duration = max(worst_duration, result.duration_s)
            exit_codes.append(result.exit_code)
            findings.extend(analyze.check_decision_placement(
                event, classification["decision"]))
            findings.extend(analyze.slow_hook_assessment(hook, result.duration_s))
            findings.extend(analyze.stderr_swallowed_assessment(result))
            if classification["outcome"] != "block":
                break
        findings.extend(analyze.stop_loop_assessment(outcomes))
        runtime = {"duration_s": round(worst_duration, 3),
                   "exit_code": exit_codes[-1] if exit_codes else None,
                   "iterations": len(outcomes)}
        worst = max(outcomes, key=lambda o: {"block": 3, "timeout": 2,
                                             "non-blocking-error": 2,
                                             "spawn-error": 2}.get(
                                                 o["outcome"], 0))
        findings.extend(analyze.fail_open_assessment(hook, worst["outcome"]))
        return runtime, findings

    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "PermissionRequest", "PermissionDenied"):
        outcomes = {}
        worst_duration = 0.0
        exit_codes = {}
        for kind, hostile in (("benign", False), ("hostile", True)):
            payload = payloads.build_payload(event, hook, project_dir,
                                             transcript_path, hostile=hostile)
            result, classification = do_run(payload)
            outcomes[kind] = classification
            worst_duration = max(worst_duration, result.duration_s)
            exit_codes[kind] = result.exit_code
            findings.extend(analyze.check_decision_placement(
                event, classification["decision"]))
            findings.extend(analyze.slow_hook_assessment(hook, result.duration_s))
            findings.extend(analyze.stderr_swallowed_assessment(result))
        runtime = {"duration_s": round(worst_duration, 3),
                   "exit_code": exit_codes,
                   "benign": outcomes["benign"]["outcome"],
                   "hostile": outcomes["hostile"]["outcome"]}
        benign_blocks = outcomes["benign"]["outcome"] == "block" or (
            analyze.decision_allows(outcomes["benign"]["decision"]) is False)
        hostile_blocks = outcomes["hostile"]["outcome"] == "block" or (
            analyze.decision_allows(outcomes["hostile"]["decision"]) is False)
        # Only a hook on an event that can block can be a dead guard:
        # PostToolUse exit 2 only shows stderr, so an always-allow
        # PostToolUse hook is observational, not broken.
        if is_guard and events_mod.can_block(event):
            if not hostile_blocks and outcomes["hostile"]["outcome"] in (
                    "allow", "non-blocking-error", "timeout", "spawn-error"):
                findings.append({
                    "code": "always-allow-guard", "severity": "error",
                    "message": "guard-shaped hook (%s) let the hostile "
                    "probe through (%s) -- it never blocks anything, so "
                    "the session is unguarded while believing it is "
                    "protected" % ("; ".join(guard_reasons),
                                   outcomes["hostile"]["detail"])})
            elif hostile_blocks and not benign_blocks:
                findings.append({
                    "code": "guard-works", "severity": "info",
                    "message": "guard blocks the hostile probe and allows "
                    "the benign one"})
            elif hostile_blocks and benign_blocks:
                findings.append({
                    "code": "blocks-everything", "severity": "info",
                    "message": "guard blocks both probes; check that "
                    "blocking the benign probe is intended"})
        worst_outcome = outcomes["hostile"]["outcome"]
        if outcomes["benign"]["outcome"] in ("timeout", "spawn-error",
                                              "non-blocking-error"):
            worst_outcome = outcomes["benign"]["outcome"]
        findings.extend(analyze.fail_open_assessment(hook, worst_outcome))
        return runtime, findings

    payload = payloads.build_payload(event, hook, project_dir, transcript_path)
    result, classification = do_run(payload)
    runtime = {"duration_s": round(result.duration_s, 3),
               "exit_code": result.exit_code,
               "outcome": classification["outcome"]}
    findings.extend(analyze.check_decision_placement(
        event, classification["decision"]))
    findings.extend(analyze.slow_hook_assessment(hook, result.duration_s))
    findings.extend(analyze.stderr_swallowed_assessment(result))
    if classification["outcome"] in ("timeout", "spawn-error",
                                     "non-blocking-error"):
        findings.append({
            "code": "hook-unhealthy", "severity": "error",
            "message": "%s hook %s: %s"
            % (event, classification["outcome"], classification["detail"])})
    findings.extend(analyze.fail_open_assessment(hook, classification["outcome"]))
    return runtime, findings


def check(project_dir, settings_file=None, include_user=True, run=True,
          test_timeout=15.0):
    """Run the whole audit; returns (results, summary)."""
    hooks, errors, sources = config.collect_hooks(
        project_dir, include_user=include_user, settings_file=settings_file)
    # The synthetic payloads point transcript_path at a real (minimal)
    # transcript file in a temp dir, so a hook that reads it behaves as
    # it would in a session instead of crashing on a missing file.
    import tempfile
    tmp = tempfile.TemporaryDirectory(prefix="hook-doctor-")
    transcript_path = os.path.join(tmp.name, "transcript.jsonl")
    with open(transcript_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "summary",
                             "summary": "hook-doctor test session"}) + "\n")
    results = []
    seen_identities = {}
    for hook in hooks:
        findings = []
        findings.extend(config.validate_handler(hook))
        findings.extend(_matcher_findings(hook))
        findings.extend(_if_matcher_contradiction(hook))
        findings.extend(_command_resolution_findings(hook, project_dir))
        script_text = _read_script_text(hook, project_dir)
        findings.extend(_jq_findings(hook, script_text))
        # The same handler in several settings files runs once.
        identity = hook.identity()
        if identity in seen_identities:
            findings.append({
                "code": "duplicate-handler", "severity": "info",
                "message": "identical handler also defined in %s; "
                "Claude Code runs it once" % seen_identities[identity]})
        else:
            seen_identities[identity] = hook.source
        executed = False
        runtime = None
        static_broken = any(
            f["code"] in ("missing-type", "unknown-type", "missing-field",
                          "unknown-event", "command-not-found",
                          "command-not-executable", "script-not-found")
            for f in findings)
        if run and hook.type == "command" and not static_broken:
            runtime, run_findings = _probe_hook(
                hook, project_dir, transcript_path, test_timeout)
            findings.extend(run_findings)
            executed = True
        elif run and hook.type != "command":
            findings.append({
                "code": "not-executable-offline", "severity": "info",
                "message": "type %r hooks cannot be executed offline; "
                "only static analysis was done" % hook.type})
        # Probes run twice (benign + hostile), so the same finding can
        # fire twice; keep one copy of each.
        seen = set()
        deduped = []
        for finding in findings:
            key = (finding["code"], finding["message"])
            if key not in seen:
                seen.add(key)
                deduped.append(finding)
        findings = deduped
        verdict = report.verdict_for(findings, executed)
        if not executed and verdict == "pass" and hook.type != "command":
            verdict = "skip"
        results.append({"hook": hook, "findings": findings,
                        "verdict": verdict, "runtime": runtime})
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0, "error": 0}
    for entry in results:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
    summary = {
        "version": __version__,
        "hooks": len(hooks),
        "sources": sources,
        "errors": errors,
        "pass": counts["pass"],
        "warn": counts["warn"],
        "fail": counts["fail"] + counts["error"],
        "skip": counts["skip"],
    }
    tmp.cleanup()
    return results, summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hook-doctor",
        description="Test and audit Claude Code hook configurations.")
    sub = parser.add_subparsers(dest="command")
    check_parser = sub.add_parser(
        "check", help="check the hooks visible from a project directory")
    check_parser.add_argument("path", nargs="?", default=".",
                              help="project directory (default: .)")
    check_parser.add_argument("--settings",
                              help="check one settings file instead of "
                              "discovering them")
    check_parser.add_argument("--no-user", action="store_true",
                              help="do not include ~/.claude/settings.json")
    check_parser.add_argument("--no-run", action="store_true",
                              help="static analysis only; do not execute hooks")
    check_parser.add_argument("--test-timeout", type=float, default=15.0,
                              help="max seconds per test run (default: 15)")
    check_parser.add_argument("--format", choices=("text", "json"),
                              default="text")
    args = parser.parse_args(argv)
    if args.command != "check":
        parser.print_help()
        return 3
    project_dir = os.path.abspath(args.path)
    if not os.path.isdir(project_dir):
        print("hook-doctor: %s is not a directory" % project_dir,
              file=sys.stderr)
        return 3
    results, summary = check(
        project_dir,
        settings_file=args.settings,
        include_user=not args.no_user,
        run=not args.no_run,
        test_timeout=args.test_timeout)
    if args.format == "json":
        print(report.render_json(results, summary), end="")
    else:
        print(report.render_text(results, summary), end="")
    if not results and not summary["errors"]:
        return 3
    if summary["fail"]:
        return 2
    if any(f["severity"] == "warning" for entry in results
           for f in entry["findings"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
