# hook-doctor

Test and audit Claude Code hook configurations — find the dead guards before a session trusts them.

## The problem

Claude Code hooks are the only deterministic guardrail in a session: skills are probabilistic, but a hook runs or it doesn't. The catch is that a broken hook fails **silently by design**. The official hooks reference is blunt about it:

- A hook whose script path is wrong "is a non-blocking error too" — the action goes ahead and the session carries on believing it is guarded.
- A timed-out hook's output is discarded, and on `PreToolUse` "the call continues through the normal permission flow, so don't count on a stalled hook to act as a gate."
- Any exit code other than 0 or 2 lets the action through, and exit 0 with an empty output is indistinguishable from a guard that checked and approved.

Real teams have hit every one of these. A November 2026 write-up on parallel-session guards puts it plainly: *"Every hook exits 0 with no output on any error. A guard that crashes must never stop work."* (dev.to, Paul Tuddenham). A Stop hook that never accepts the session is done loops the agent: *"A hook exiting 2 blocks the action. A bad Stop hook loops the agent forever."* (PostGoo). And the incident that motivated this tool's sibling check: seven parallel subagents, one buggy test starting four real processes, two upload workers defaulting to the production ingest URL — *"agent fixed the bug 7 seconds later and never mentioned the side effect"* (whatagentscando.com, Oct 29, 2026). The fix that stuck in that team's practice was hooks as checks: *"a hook that blocks the action"* (getrush.ai, Nov 7, 2026) — but only if the hook actually works.

There was no tool that tests the hooks you already have. `claude plugin validate` checks skill frontmatter, not hooks. Hook frameworks (captain-hook, claude-hooks) test hooks written *in their framework*; they do not audit an existing `settings.json`. hook-doctor does: it reads your settings files, plugin `hooks.json` and skill-frontmatter hooks, and tests each one against the documented behaviour.

## What it checks

For every hook in `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json`, plugin `hooks/hooks.json` and skill/subagent frontmatter:

**Static analysis (no execution)**
- Unknown events (Claude Code documents 33) and unknown handler types; missing required fields (`command`, `url`, `server`+`tool`, `prompt`).
- `if` on a non-tool event — the docs say such a hook *never runs*.
- A matcher and an `if` that name different tools — a contradiction, so the hook can never run.
- Matchers that can never match (exact-list values outside the event's documented domain, e.g. a `SessionEnd` matcher of `banana`); invalid regex matchers.
- `async: true` on a blocking event (an async hook cannot block); `once` outside skill frontmatter (ignored); `onFailure` on a type that ignores it; bad timeouts.
- Command resolution: the script or executable must exist and be runnable. A missing script is a dead guard. This includes scripts passed to an interpreter (`sh .claude/hooks/guard.sh`): dash exits 2 when it cannot open a script, so without this check a missing script would look like a guard that blocks everything.
- `jq` usage without `jq` on PATH (the classic silent no-op: the hook fails on every run).

**Live probes (default; `--no-run` skips)**
- Each `command` hook runs in a sandbox temp dir with a scrubbed environment (no credentials are passed through) and the documented JSON payload for its event.
- Tool-event hooks get two probes: a benign one (`git status`) and a hostile one (`rm -rf …`, or a `.env` path for file tools). A guard-shaped hook that lets the hostile probe through is reported as `always-allow-guard`.
- Stop hooks get a livelock simulation: up to 8 iterations (the documented consecutive-block cap), flipping `stop_hook_active` to true after the first block. A hook that keeps blocking ignores the flag and would loop to the cap.
- SessionEnd hooks run against their documented 1.5s shared budget.
- Outcomes are classified with the docs' exit-code / stdout matrix (exit 0 + valid JSON applies; exit 0 + plain text is success; exit 0 + malformed JSON is a non-blocking error; exit 2 blocks where the event allows it; any other code with valid JSON lets the JSON decide).
- JSON decisions are checked for placement: a top-level `permissionDecision` on `PreToolUse` is ignored by Claude Code — it belongs inside `hookSpecificOutput`.
- Fail-open analysis: a blocking-event hook without `onFailure: "block"` (Claude Code v2.1.295+) is flagged — healthy ones as a risk warning, crashed/timed-out ones as an error.
- Slow hooks (>1s on per-tool-call events) and stderr printed on exit 0 (which Claude Code never shows the model) are flagged.

HTTP, MCP-tool, prompt and agent hooks get static analysis only — they cannot be executed offline — and are reported as SKIP.

## Usage

```bash
pip install .
hook-doctor check                     # current project, incl. ~/.claude/settings.json
hook-doctor check /path/to/project --no-user
hook-doctor check --settings .claude/settings.json
hook-doctor check --no-run            # static analysis only
hook-doctor check --format json       # machine-readable output
```

Exit codes: 0 all hooks healthy · 1 warnings only · 2 a hook fails · 3 usage error (no hooks found, unreadable target).

## Demo

`examples/broken-hooks/` (8 hooks, 7 fail) and `examples/healthy-hooks/` (3 hooks, all pass):

```bash
hook-doctor check examples/healthy-hooks --no-user   # exit 0
hook-doctor check examples/broken-hooks --no-user    # exit 2
```

The broken example plants one of each failure: a missing script, an always-allow guard, a misplaced JSON decision, a matcher/`if` contradiction, an `if` on a Stop hook, a SessionEnd hook over its 1.5s budget, a Stop hook that loops, and a prompt hook (SKIP).

## How it differs

| Tool | What it does |
| --- | --- |
| `claude plugin validate` | Validates skill frontmatter only |
| captain-hook / claude-hooks | Test hooks written in their own framework |
| claude-code-kit `doctor` | Environment checks (git, jq, hook scripts exist) |
| **hook-doctor** | Tests *any existing* hook config — settings, plugins, skills — against the documented hook contract, with live probes and livelock simulation |

## Honest limitations

- **Hook payloads are synthetic.** Common fields and per-event fields come from the official hooks reference (read 2026-11-11); a few shapes the docs do not spell out (notably `tool_response`) are best-effort. A hook that depends on a field hook-doctor does not send will behave differently in a real session — the tool says which fields it sends.
- **Guard-shaped is a heuristic.** Whether a hook is *meant* to block is inferred from its name, command text, `if` filter and `onFailure` setting; every such finding names its reasons so a wrong guess is visible. Observational hooks are never failed for allowing.
- **Regex matchers are approximated.** Claude Code evaluates them as JavaScript regexes; hook-doctor uses Python `re.search`. JS-only constructs are reported as uncheckable, not broken.
- **Sandbox, not a session.** Hooks run in a temp dir with a scrubbed env. Scripts that rely on project-relative paths beyond the entry script, on the real transcript, or on session state will see a different world; the entry script path is resolved to the project for the test run.
- **No live Claude Code run.** This tests the hook contract offline. It cannot prove a hook fires at the right moment in a real session — only that the hook, when fed the documented input, does what its author intended.
- **Version drift.** Hook behaviour is versioned (e.g. `onFailure` needs v2.1.295+). Findings cite the documented behaviour as of Nov 2026; a newer Claude Code may change the contract.

## Tests

```bash
python3 -m unittest discover -s tests   # 103 tests
```

## License

MIT — Copyright (c) 2026 Shouvik Maji
