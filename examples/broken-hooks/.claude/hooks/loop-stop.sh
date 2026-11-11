#!/bin/sh
# A Stop hook that never accepts the session is done: it blocks on every
# call, ignoring stop_hook_active, so it would loop to the 8-block cap.
echo "checks pending" >&2
exit 2
