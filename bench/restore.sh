#!/usr/bin/env bash
# Emergency cleanup after an interrupted run: kills dperf/nginx, removes the test
# bond/vxlan, binds the test ports back to the kernel driver.  ./restore.sh client|server
source "$(dirname "$0")/_common.sh"
role="${1:-}"
[ "$role" = client ] || [ "$role" = server ] || { echo "usage: $0 client|server" >&2; exit 2; }
fbench restore --role "$role"
