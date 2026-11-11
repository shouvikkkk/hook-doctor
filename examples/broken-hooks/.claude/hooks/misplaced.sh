#!/bin/sh
# Prints a decision at the top level, where Claude Code ignores it on
# PreToolUse (it belongs inside hookSpecificOutput).
printf '{"permissionDecision": "deny"}'
