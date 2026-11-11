"""Execute a command hook the way Claude Code would -- minus the session.

Safety rules for the test run (the hook under test is arbitrary user
code, so hook-doctor treats it as untrusted):
- It runs in a fresh temporary directory, never the real project, so a
  hook that writes files cannot touch the repo it guards.
- The environment is scrubbed: only PATH, HOME, TMPDIR, LANG and the
  documented CLAUDE_* placeholders survive. Credentials (notably
  ANTHROPIC_API_KEY and friends) are never passed through -- Claude
  Code itself strips credentials from hook environments in HIPAA
  sessions, and a tester has no business handing them over.
- A per-run timeout caps every execution; a hook that hangs is reported
  as TIMEOUT, not left running.
- Shell form runs through ``sh -c`` (macOS/Linux, matching the docs);
  exec form (``args`` set) spawns the executable directly with no shell.
  ``${CLAUDE_PROJECT_DIR}`` etc. are substituted with the temp dir so
  path references resolve inside the sandbox.
"""

import json
import os
import shlex
import subprocess
import tempfile
import time

_PLACEHOLDERS = ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")

_SAFE_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
                  "USER", "LOGNAME", "SHELL", "TERM")


class RunResult:
    def __init__(self, exit_code, stdout, stderr, duration_s, timed_out,
                 spawn_error=None):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_s = duration_s
        self.timed_out = timed_out
        self.spawn_error = spawn_error

    @property
    def stdout_json(self):
        """Parsed stdout when it is exactly one JSON object, else None."""
        text = self.stdout.strip()
        if not (text.startswith("{") and text.endswith("}")):
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @property
    def stdout_json_error(self):
        """Why stdout is not usable JSON, or None when it is fine."""
        text = self.stdout.strip()
        if text == "":
            return None
        if not (text.startswith("{") and text.endswith("}")):
            return "stdout is plain text, not a JSON object"
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return "stdout is not valid JSON: %s" % exc
        if not isinstance(parsed, dict):
            return "stdout JSON is not an object"
        return None


def _substitute_placeholders(text, values):
    for key, value in values.items():
        text = text.replace("${%s}" % key, value)
    return text


def _sandbox_env(workdir):
    env = {}
    for key in _SAFE_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    env["CLAUDE_PROJECT_DIR"] = workdir
    env["CLAUDE_PLUGIN_ROOT"] = workdir
    env["CLAUDE_PLUGIN_DATA"] = workdir
    return env


def resolve_command_path(command, workdir):
    """Resolve the executable a shell-form command would run.

    Returns (kind, detail): kind is "file" (an existing path),
    "path" (found on PATH), "builtin" (a shell builtin such as ``echo``),
    or "missing" (nothing found). Only the first token is resolved;
    pipes and friends are not followed.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return "missing", "empty command"
    first = tokens[0]
    if first in ("echo", "printf", "test", "[", "true", "false", "cd",
                 "export", "exit", "set", "source", ".", "pwd", "sleep"):
        return "builtin", first
    candidate = os.path.join(workdir, first) if not os.path.isabs(first) \
        else first
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return "file", candidate
    if os.path.isfile(candidate):
        return "file-not-executable", candidate
    if "/" in first:
        return "missing", first
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, first)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return "path", candidate
    return "missing", first


def run_command_hook(handler, payload, timeout_s, workdir=None):
    """Run one command hook with the given JSON payload on stdin.

    Returns a RunResult. ``workdir`` is the sandbox directory; when
    omitted a fresh temp dir is created and removed afterwards.
    """
    own_dir = workdir is None
    if own_dir:
        tmp = tempfile.TemporaryDirectory(prefix="hook-doctor-")
        workdir = tmp.name
    else:
        tmp = None
    start = time.monotonic()
    try:
        env = _sandbox_env(workdir)
        placeholders = {key: workdir for key in _PLACEHOLDERS}
        argv = None
        shell_command = None
        if handler.get("args") is not None:
            command = _substitute_placeholders(str(handler["command"]), placeholders)
            argv = [command] + [
                _substitute_placeholders(str(a), placeholders)
                for a in handler["args"]
            ]
        else:
            shell_command = _substitute_placeholders(
                str(handler["command"]), placeholders)
        data = json.dumps(payload).encode("utf-8")
        try:
            if argv is not None:
                proc = subprocess.run(
                    argv, input=data, capture_output=True,
                    timeout=timeout_s, cwd=workdir, env=env)
            else:
                proc = subprocess.run(
                    ["sh", "-c", shell_command], input=data,
                    capture_output=True, timeout=timeout_s,
                    cwd=workdir, env=env)
        except FileNotFoundError as exc:
            return RunResult(None, "", "", time.monotonic() - start, False,
                             spawn_error="cannot start hook: %s" % exc)
        except subprocess.TimeoutExpired as exc:
            return RunResult(None,
                             (exc.stdout or b"").decode("utf-8", "replace"),
                             (exc.stderr or b"").decode("utf-8", "replace"),
                             time.monotonic() - start, True)
        return RunResult(proc.returncode,
                         proc.stdout.decode("utf-8", "replace"),
                         proc.stderr.decode("utf-8", "replace"),
                         time.monotonic() - start, False)
    finally:
        if tmp is not None:
            tmp.cleanup()
