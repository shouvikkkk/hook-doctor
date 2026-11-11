"""Render hook-doctor results as text or JSON.

The text report is grouped per hook: verdict first, then findings.
Severity order: error > warning > info. A hook with no error and no
warning is PASS. Verdicts:
- PASS: the hook ran (or was statically sound) and behaves.
- WARN: warnings only.
- FAIL: at least one error finding.
- SKIP: not executable offline (http/mcp_tool/prompt/agent) -- static
  analysis only; the verdict says what was and was not checked.
- ERROR: the hook configuration itself is broken (unknown event/type,
  missing required field).
"""

import json

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

_VERDICT_WORD = {
    "pass": "PASS",
    "warn": "WARN",
    "fail": "FAIL",
    "skip": "SKIP",
    "error": "ERROR",
}


def verdict_for(findings, executed):
    """Overall verdict for one hook from its findings."""
    severities = [f["severity"] for f in findings]
    if "error" in severities:
        return "fail" if executed else "error"
    if "warning" in severities:
        return "warn"
    return "pass"


def render_text(results, summary):
    lines = []
    lines.append("hook-doctor %s" % summary["version"])
    lines.append("scanned %d hook(s) from %d file(s)"
                 % (summary["hooks"], len(summary["sources"])))
    lines.append("")
    for entry in results:
        hook = entry["hook"]
        verdict = _VERDICT_WORD[entry["verdict"]]
        lines.append("[%s] %s  (%s)"
                     % (verdict, hook.label(), hook.source))
        if entry.get("runtime"):
            lines.append("      ran in %.2fs, exit %s"
                         % (entry["runtime"]["duration_s"],
                            entry["runtime"]["exit_code"]))
        for finding in sorted(entry["findings"],
                              key=lambda f: _SEVERITY_RANK[f["severity"]]):
            mark = {"error": "!", "warning": "~", "info": "i"}[finding["severity"]]
            lines.append("      %s [%s] %s"
                         % (mark, finding["code"], finding["message"]))
        lines.append("")
    lines.append("summary: %d pass, %d warn, %d fail, %d skip/error"
                 % (summary["pass"], summary["warn"], summary["fail"],
                    summary["skip"]))
    if summary["errors"]:
        lines.append("")
        lines.append("configuration errors:")
        for error in summary["errors"]:
            lines.append("  ! %s" % error)
    return "\n".join(lines) + "\n"


def render_json(results, summary):
    payload = {
        "tool": "hook-doctor",
        "version": summary["version"],
        "summary": summary,
        "hooks": [
            {
                "event": entry["hook"].event,
                "matcher": entry["hook"].matcher,
                "type": entry["hook"].type,
                "source": entry["hook"].source,
                "source_kind": entry["hook"].source_kind,
                "verdict": entry["verdict"],
                "runtime": entry.get("runtime"),
                "findings": entry["findings"],
            }
            for entry in results
        ],
    }
    return json.dumps(payload, indent=2) + "\n"
