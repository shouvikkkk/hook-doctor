#!/bin/sh
# A Stop hook that respects the continuation flag: it blocks once (work
# may be unfinished), then allows when stop_hook_active is true.
input=$(cat)
case "$input" in
  *stop_hook_active*true*)
    exit 0
    ;;
  *)
    echo "Final checks not done yet." >&2
    exit 2
    ;;
esac
