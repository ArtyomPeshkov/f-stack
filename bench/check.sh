#!/usr/bin/env bash
# Pre-flight check of this machine: ./check.sh client   or   ./check.sh server
source "$(dirname "$0")/_common.sh"
role="${1:-}"
[ "$role" = client ] || [ "$role" = server ] || { echo "usage: $0 client|server" >&2; exit 2; }
shift
fbench check --role "$role" "$@"
