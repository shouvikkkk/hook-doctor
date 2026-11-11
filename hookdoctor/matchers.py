"""Matcher and ``if``-filter semantics, from the official hooks reference.

Matcher rules (docs, "Matcher patterns"):
- ``"*"``, ``""`` or omitted: match all.
- Only letters, digits, ``_``, ``-``, spaces, ``,`` and ``|``: exact
  string, or a list of exact strings separated by ``|`` or ``,``.
- Anything else: a JavaScript regular expression, unanchored
  (``RegExp.prototype.test`` semantics). hook-doctor approximates it with
  Python ``re.search`` and says so in the report; JS and Python regex
  dialects differ on a few constructs (e.g. lookbehind is fine in both,
  but ``\\d`` inside a character class behaves the same; the main gap is
  JS-only syntax such as ``(?<=...)`` with variable length, which Python
  rejects -- in that case the matcher is reported as uncheckable, not as
  broken).
- FileChanged and StopFailure use a narrower exact-match set
  (letters, digits, ``_`` and ``|`` only); a hyphen, space or comma
  keeps those on the regex path, and only ``|`` separates alternatives.
  FileChanged matchers are literal filenames, so dots and slashes are
  exact there too (the docs' own example is ``.envrc|.env``).

``if`` rules (docs, "How `if` patterns match Bash commands"):
- Exactly one permission rule, e.g. ``Bash(git *)`` or ``Edit(*.ts)``.
- Only evaluated on tool events (PreToolUse, PostToolUse,
  PostToolUseFailure, PermissionRequest, PermissionDenied). On any other
  event a hook with ``if`` set never runs.
- For Bash: leading ``VAR=value`` assignments are stripped; each
  subcommand (split on ``&&``, ``||``, ``;``, ``|``) is checked;
  commands inside ``$()`` and backticks are checked too; a pattern that
  specifies more than the command name runs the hook anyway on
  ``$()``/backticks/``$VAR``; when Claude Code cannot determine which
  commands the input runs, it runs the hook regardless (best-effort).
"""

import re
import shlex

_EXACT_CHARS = re.compile(r"^[A-Za-z0-9_\- ,|]*$")
# FileChanged and StopFailure use a narrower exact-match set. For
# StopFailure the values are snake_case error types; for FileChanged
# the values are literal filenames, so dots and slashes are exact too
# (the docs' own example is ".envrc|.env").
_NARROW_EXACT_CHARS = re.compile(r"^[A-Za-z0-9_|]*$")
_FILECHANGED_EXACT_CHARS = re.compile(r"^[A-Za-z0-9_|./-]*$")


def matcher_kind(matcher, event):
    """Classify a matcher string: "all", "exact", or "regex"."""
    if matcher is None or matcher == "" or matcher == "*":
        return "all"
    if event == "StopFailure":
        if _NARROW_EXACT_CHARS.match(matcher):
            return "exact"
        return "regex"
    if event == "FileChanged":
        if _FILECHANGED_EXACT_CHARS.match(matcher):
            return "exact"
        return "regex"
    if _EXACT_CHARS.match(matcher):
        return "exact"
    return "regex"


def split_exact_list(matcher):
    """Split an exact-list matcher into its alternatives."""
    parts = re.split(r"[|,]", matcher)
    return [p.strip() for p in parts if p.strip()]


def evaluate_matcher(matcher, value, event):
    """Return (matches, note).

    ``matches`` is True/False, or None when the matcher cannot be
    evaluated (e.g. a JS-only regex construct Python rejects).
    """
    kind = matcher_kind(matcher, event)
    if kind == "all":
        return True, "matcher matches everything"
    if kind == "exact":
        return value in split_exact_list(matcher), "exact match"
    try:
        return re.search(matcher, value) is not None, "regex match"
    except re.error as exc:
        return None, "regex not evaluable in Python: %s" % exc


def _strip_leading_assignments(tokens):
    """Drop leading VAR=value tokens, as Claude Code does for Bash."""
    out = list(tokens)
    while out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]):
        out.pop(0)
    return out


def _split_subcommands(command):
    """Split a shell command on &&, ||, ; and | (naive, quote-aware)."""
    parts = []
    current = []
    i = 0
    in_single = in_double = False
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif not in_single and not in_double and command[i:i + 2] in ("&&", "||"):
            parts.append("".join(current))
            current = []
            i += 1
        elif not in_single and not in_double and ch in ";|":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _extract_command_words(command):
    """All command words a Bash input could run.

    Returns (words, uncertain): ``words`` are the first token of every
    subcommand plus every command word found inside ``$()`` and backticks;
    ``uncertain`` is True when the input contains a form we cannot fully
    resolve (``$VAR`` as a command, unparseable quoting), in which case
    Claude Code runs the hook regardless.
    """
    words = []
    uncertain = False
    # Commands inside $() and backticks.
    for inner in re.findall(r"\$\(([^()]*)\)", command):
        sub_words, sub_uncertain = _extract_command_words(inner)
        words.extend(sub_words)
        uncertain = uncertain or sub_uncertain
    for inner in re.findall(r"`([^`]*)`", command):
        sub_words, sub_uncertain = _extract_command_words(inner)
        words.extend(sub_words)
        uncertain = uncertain or sub_uncertain
    outer = re.sub(r"\$\([^()]*\)", "", command)
    outer = re.sub(r"`[^`]*`", "", outer)
    for sub in _split_subcommands(outer):
        try:
            tokens = shlex.split(sub, posix=True)
        except ValueError:
            uncertain = True
            tokens = sub.split()
        tokens = _strip_leading_assignments(tokens)
        if not tokens:
            continue
        first = tokens[0]
        if first.startswith("$"):
            uncertain = True
            continue
        words.append(first)
    return words, uncertain


def _wildcard_match(pattern, value):
    """Glob match where ``*`` matches any run of characters.

    A trailing `` *`` also matches the bare command: per the docs,
    ``Bash(mvn *)`` matches the bare ``mvn`` invocation too.
    """
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    if re.match(regex, value):
        return True
    if pattern.endswith(" *"):
        return _wildcard_match(pattern[:-2], value)
    return False


def _parse_if_rule(rule):
    """Parse one permission rule into (tool, spec) or None if malformed.

    ``Bash(git *)`` -> ("Bash", "git *"); ``Read`` -> ("Read", None).
    """
    rule = rule.strip()
    m = re.match(r"^([A-Za-z0-9_]+)\((.*)\)$", rule, re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    if re.match(r"^[A-Za-z0-9_]+$", rule):
        return rule, None
    return None


def evaluate_if(rule, tool_name, tool_input):
    """Simulate Claude Code's ``if`` filter for one tool call.

    Returns (runs, reason): ``runs`` is True when the hook would run,
    False when it would not, None when the rule is malformed.
    """
    parsed = _parse_if_rule(rule)
    if parsed is None:
        return None, "malformed `if` rule: %r" % rule
    rule_tool, spec = parsed
    if rule_tool != tool_name:
        return False, "tool %s does not match `if` tool %s" % (tool_name, rule_tool)
    if spec is None or spec == "":
        return True, "`if` matches the whole tool"
    if tool_name == "Bash":
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "") or ""
        if _wildcard_match(spec, command):
            return True, "Bash input matches `if` pattern"
        words, uncertain = _extract_command_words(command)
        # A pattern naming more than the command runs the hook anyway
        # on $()/backtick/$VAR content the matcher cannot resolve.
        pattern_words = spec.split()
        if len(pattern_words) > 1 and uncertain:
            return True, "`if` pattern is best-effort: unresolvable command content runs the hook anyway"
        for word in words:
            if _wildcard_match(spec, word):
                return True, "subcommand %r matches `if` pattern" % word
            if len(pattern_words) > 1:
                # "git push" style: match command word + first arg.
                try:
                    tokens = shlex.split(command, posix=True)
                except ValueError:
                    tokens = command.split()
                tokens = _strip_leading_assignments(tokens)
                joined = " ".join(tokens[: len(pattern_words)])
                if _wildcard_match(spec, joined):
                    return True, "command %r matches `if` pattern" % joined
        return False, "no subcommand of the Bash input matches `if` pattern"
    # File tools: match the path/pattern against file_path.
    if isinstance(tool_input, dict):
        path = tool_input.get("file_path", "") or ""
        if _wildcard_match(spec, path) or _wildcard_match(spec, path.split("/")[-1]):
            return True, "file path matches `if` pattern"
        return False, "file path does not match `if` pattern"
    return False, "no tool input to match `if` against"
