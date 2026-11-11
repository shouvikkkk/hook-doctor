#!/bin/sh
# Block rm -rf, allow everything else. Pure shell (no jq dependency):
# read the JSON payload, look for the destructive command in it.
input=$(cat)
case "$input" in
  *rm\ -rf*)
    echo "Blocked: rm -rf is not allowed in this project." >&2
    exit 2
    ;;
  *)
    exit 0
    ;;
esac
