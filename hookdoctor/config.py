"""Load hook configurations from every place Claude Code reads them.

Sources, in the order Claude Code merges them (docs, "Hook locations"):
- user settings: ``~/.claude/settings.json``
- project settings: ``<project>/.claude/settings.json``
- project local settings: ``<project>/.claude/settings.local.json``
- plugin hooks: ``hooks/hooks.json`` inside an enabled plugin
- skill/subagent frontmatter: a ``hooks:`` key in SKILL.md / agent
  frontmatter, in the same configuration format as settings-based hooks

The configuration shape is three levels: event -> matcher group ->
handlers. A handler is a dict with a ``type`` (command/http/mcp_tool/
prompt/agent) plus type-specific fields. Only ``type: "command"`` hooks
can be executed offline by hook-doctor; the others get static analysis
and an explicit "not executable here" note.

Frontmatter is parsed with a small YAML-subset parser (mappings, lists,
scalars, quoted strings, block scalars are rejected). Full YAML is out
of scope for a stdlib tool; an unparseable frontmatter ``hooks:`` block
is reported as a finding, never a crash.
"""

import json
import os
import re

from . import events as events_mod

SETTINGS_FILENAMES = ("settings.json", "settings.local.json")

# Fields hook-doctor understands on a handler. Unknown fields are
# ignored (the docs evolve; a new field is not evidence of a bug).
_COMMON_FIELDS = {"type", "if", "timeout", "statusMessage", "once"}
_TYPE_FIELDS = {
    "command": {"command", "args", "async", "asyncRewake", "shell", "onFailure"},
    "http": {"url", "headers", "allowedEnvVars", "onFailure"},
    "mcp_tool": {"server", "tool", "input"},
    "prompt": {"prompt", "model", "continueOnBlock"},
    "agent": {"prompt", "model"},
}

# Required fields per handler type (docs).
_REQUIRED_FIELDS = {
    "command": {"command"},
    "http": {"url"},
    "mcp_tool": {"server", "tool"},
    "prompt": {"prompt"},
    "agent": {"prompt"},
}


class Hook:
    """One registered hook handler."""

    def __init__(self, event, matcher, handler, source, source_kind):
        self.event = event
        self.matcher = matcher  # "" means match all
        self.handler = handler  # dict as written in the config
        self.source = source  # file path the hook came from
        self.source_kind = source_kind  # settings | plugin | skill | subagent

    @property
    def type(self):
        return self.handler.get("type")

    @property
    def command(self):
        return self.handler.get("command")

    def label(self):
        cmd = self.handler.get("command") or self.handler.get("url") \
            or self.handler.get("tool") or self.handler.get("prompt", "")[:40]
        return "%s %s %s" % (self.event, self.matcher or "*", str(cmd)[:60])

    def identity(self):
        """Handlers defined identically in several settings files run once."""
        return (self.event, self.matcher,
                json.dumps(self.handler, sort_keys=True, default=str))


class ConfigError(Exception):
    """A settings file that cannot be read or parsed."""


def load_settings_file(path):
    """Parse one settings JSON file; returns (hooks, errors).

    ``hooks`` is a list of Hook objects; ``errors`` lists human-readable
    problems with the file itself (bad JSON, wrong shape).
    """
    hooks = []
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return hooks, ["cannot read %s: %s" % (path, exc)]
    if not isinstance(data, dict):
        return hooks, ["%s: top level is not a JSON object" % path]
    raw = data.get("hooks")
    if raw is None:
        return hooks, []
    if not isinstance(raw, dict):
        return hooks, ["%s: `hooks` is not an object" % path]
    hooks.extend(_hooks_from_mapping(raw, path, "settings"))
    return hooks, errors


def load_hooks_json(path):
    """Parse a plugin ``hooks/hooks.json`` file."""
    hooks = []
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return hooks, ["cannot read %s: %s" % (path, exc)]
    if not isinstance(data, dict):
        return hooks, ["%s: top level is not a JSON object" % path]
    raw = data.get("hooks")
    if raw is None:
        return hooks, []
    if not isinstance(raw, dict):
        return hooks, ["%s: `hooks` is not an object" % path]
    hooks.extend(_hooks_from_mapping(raw, path, "plugin"))
    return hooks, errors


def _hooks_from_mapping(raw, path, kind):
    hooks = []
    for event, groups in raw.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "") or ""
            for handler in group.get("hooks", []):
                if isinstance(handler, dict):
                    hooks.append(Hook(event, matcher, handler, path, kind))
    return hooks


# ---------------------------------------------------------------------------
# Minimal YAML-subset parser for skill/subagent frontmatter.
# Handles mappings, lists ("- item"), scalars, single/double-quoted
# strings and explicit nulls -- the shapes a `hooks:` block takes.
# Anything else (anchors, block scalars, flow collections) raises
# ValueError and the caller reports the frontmatter as unparseable.
# ---------------------------------------------------------------------------

def _yaml_scalar(text):
    text = text.strip()
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if (text.startswith('"') and text.endswith('"')) or \
            (text.startswith("'") and text.endswith("'")):
        try:
            return json.loads(text) if text.startswith('"') \
                else text[1:-1].replace("''", "'")
        except ValueError:
            return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _yaml_parse_block(lines, indent):
    """Parse an indented YAML block; returns (value, lines consumed)."""
    # Skip blank lines / comments.
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == "" or s.startswith("#"):
            i += 1
        else:
            break
    if i >= len(lines):
        return None, i
    first = lines[i]
    cur = len(first) - len(first.lstrip(" "))
    if cur < indent:
        return None, 0
    if first.lstrip(" ").startswith("- ") or first.lstrip(" ").rstrip() == "-":
        return _yaml_parse_list(lines, cur)
    return _yaml_parse_map(lines, cur)


def _yaml_parse_map(lines, indent):
    result = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == "" or raw.strip().startswith("#"):
            i += 1
            continue
        cur = len(raw) - len(raw.lstrip(" "))
        if cur < indent:
            break
        if cur > indent:
            raise ValueError("unexpected indent at line: %r" % raw)
        stripped = raw.strip()
        if stripped.startswith("- "):
            break
        if ":" not in stripped:
            raise ValueError("not a mapping line: %r" % raw)
        key, _, rest = stripped.partition(":")
        key = key.strip().strip('"').strip("'")
        rest = rest.strip()
        if rest in ("|", ">", "|+", "|-", ">+", ">-"):
            raise ValueError("block scalars not supported: %r" % raw)
        if rest.startswith("[") or rest.startswith("{"):
            raise ValueError("flow collections not supported: %r" % raw)
        if rest == "":
            value, consumed = _yaml_parse_block(lines[i + 1:], indent + 1)
            result[key] = value
            i += 1 + consumed
        else:
            result[key] = _yaml_scalar(rest)
            i += 1
    return result, i


def _yaml_parse_list(lines, indent):
    result = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == "" or raw.strip().startswith("#"):
            i += 1
            continue
        cur = len(raw) - len(raw.lstrip(" "))
        if cur < indent:
            break
        if cur > indent:
            raise ValueError("unexpected indent at line: %r" % raw)
        stripped = raw.strip()
        if not (stripped.startswith("- ") or stripped == "-"):
            break
        item_text = stripped[1:].strip()
        if item_text == "":
            value, consumed = _yaml_parse_block(lines[i + 1:], indent + 1)
            result.append(value)
            i += 1 + consumed
        elif ":" in item_text and not item_text.startswith(('"', "'")):
            # Inline map start: "- key: value" -- rewrite as a map block.
            sub = [(" " * (indent + 2)) + item_text] + lines[i + 1:]
            value, consumed = _yaml_parse_map(sub, indent + 2)
            result.append(value)
            i += consumed
        else:
            result.append(_yaml_scalar(item_text))
            i += 1
    return result, i


def parse_frontmatter_hooks(path):
    """Extract hooks from a SKILL.md / agent file's frontmatter.

    Returns (hooks, errors). Only the ``hooks:`` key is read.
    """
    hooks = []
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return hooks, ["cannot read %s: %s" % (path, exc)]
    if not text.startswith("---"):
        return hooks, []
    end = text.find("\n---", 3)
    if end == -1:
        return hooks, ["%s: unterminated frontmatter" % path]
    fm_text = text[3:end]
    try:
        parsed, _ = _yaml_parse_block(fm_text.splitlines(), 0)
    except ValueError as exc:
        return hooks, ["%s: frontmatter not parseable: %s" % (path, exc)]
    if not isinstance(parsed, dict):
        return hooks, []
    raw = parsed.get("hooks")
    if raw is None:
        return hooks, []
    if not isinstance(raw, dict):
        return hooks, ["%s: frontmatter `hooks` is not a mapping" % path]
    kind = "skill" if "skills" in path.replace(os.sep, "/") else "subagent"
    hooks.extend(_hooks_from_mapping(raw, path, kind))
    return hooks, errors


def collect_hooks(project_dir, include_user=True, settings_file=None,
                  plugins_dir=None):
    """Collect every hook visible from a project directory.

    Returns (hooks, errors, sources): ``sources`` lists the files read.
    """
    hooks = []
    errors = []
    sources = []
    candidates = []
    if settings_file:
        candidates.append((settings_file, "settings"))
    else:
        if include_user:
            candidates.append(
                (os.path.expanduser("~/.claude/settings.json"), "settings"))
        for name in SETTINGS_FILENAMES:
            candidates.append(
                (os.path.join(project_dir, ".claude", name), "settings"))
    for path, _kind in candidates:
        if os.path.isfile(path):
            found, errs = load_settings_file(path)
            hooks.extend(found)
            errors.extend(errs)
            sources.append(path)
    # Plugin hooks.json files under the project and (if given) a plugins dir.
    search_roots = [os.path.join(project_dir, ".claude", "plugins")]
    if plugins_dir:
        search_roots.append(plugins_dir)
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if "hooks.json" in files:
                path = os.path.join(dirpath, "hooks.json")
                # Only accept hooks/hooks.json (a plugin's hook file).
                if os.path.basename(dirpath) == "hooks":
                    found, errs = load_hooks_json(path)
                    hooks.extend(found)
                    errors.extend(errs)
                    sources.append(path)
    # Skill / subagent frontmatter hooks.
    for sub in ("skills", "agents"):
        base = os.path.join(project_dir, ".claude", sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fname in files:
                if fname.endswith(".md"):
                    path = os.path.join(dirpath, fname)
                    found, errs = parse_frontmatter_hooks(path)
                    hooks.extend(found)
                    errors.extend(errs)
                    if found or errs:
                        sources.append(path)
    return hooks, errors, sources


def validate_handler(hook):
    """Static validation of one handler; returns a list of findings.

    Each finding is a dict: {code, severity, message}. Severity is one of
    "error", "warning", "info". This covers config shape only; runtime
    behaviour is judged by the runner/analyser.
    """
    findings = []
    handler = hook.handler
    htype = handler.get("type")
    if htype is None:
        findings.append({"code": "missing-type", "severity": "error",
                         "message": "handler has no `type` field"})
        return findings
    if htype not in _TYPE_FIELDS:
        findings.append({"code": "unknown-type", "severity": "error",
                         "message": "unknown handler type %r (expected one "
                         "of command, http, mcp_tool, prompt, agent)" % htype})
        return findings
    for field in _REQUIRED_FIELDS[htype]:
        if field not in handler or handler[field] in (None, ""):
            findings.append({"code": "missing-field", "severity": "error",
                             "message": "type %r requires field %r"
                             % (htype, field)})
    timeout = handler.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float))
                                or isinstance(timeout, bool)
                                or timeout <= 0):
        findings.append({"code": "bad-timeout", "severity": "error",
                         "message": "`timeout` must be a positive number of "
                         "seconds, got %r" % (timeout,)})
    on_failure = handler.get("onFailure")
    if on_failure is not None and htype not in ("command", "http"):
        findings.append({"code": "onfailure-unsupported", "severity": "warning",
                         "message": "`onFailure` is only honoured on command "
                         "and http hooks, not %r" % htype})
    elif on_failure is not None and on_failure not in ("continue", "block"):
        findings.append({"code": "bad-onfailure", "severity": "error",
                         "message": "`onFailure` must be \"continue\" or "
                         "\"block\", got %r" % (on_failure,)})
    if handler.get("async") and events_mod.can_block(hook.event):
        findings.append({"code": "async-blocking", "severity": "warning",
                         "message": "`async: true` means this hook cannot "
                         "block or rewrite the result on %s; a guard that "
                         "must stop the action cannot be async"
                         % hook.event})
    if handler.get("once") and hook.source_kind != "skill":
        findings.append({"code": "once-ignored", "severity": "warning",
                         "message": "`once` is only honoured for hooks "
                         "declared in skill frontmatter; it is ignored here"})
    if hook.event not in events_mod.EVENTS:
        findings.append({"code": "unknown-event", "severity": "error",
                         "message": "unknown hook event %r (Claude Code "
                         "documents 33 events)" % hook.event})
        return findings
    if handler.get("if") is not None and hook.event not in (
            "PreToolUse", "PostToolUse", "PostToolUseFailure",
            "PermissionRequest", "PermissionDenied"):
        findings.append({"code": "if-never-runs", "severity": "error",
                         "message": "`if` is only evaluated on tool events; "
                         "on %s this hook never runs" % hook.event})
    return findings
