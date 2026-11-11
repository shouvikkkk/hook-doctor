"""hook-doctor: test and audit Claude Code hook configurations.

A hook is the only deterministic guardrail in a Claude Code session, yet a
broken hook fails silently by design: a missing script is a non-blocking
error, a timeout discards the hook's output and the action goes ahead, and
any exit code other than 0 or 2 lets the action through. hook-doctor finds
those dead guards before a session trusts them.
"""

__version__ = "0.1.0"
