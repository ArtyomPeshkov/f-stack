#!/usr/bin/env bash
# Scenario 2: dperf client -> nginx (Linux kernel stack vs F-Stack).
# Run on the CLIENT machine; the agent must run on the server (./agent.sh) unless
# [control] ssh is set. Extra arguments go to "fbench.py run", e.g.:
#   ./run_scenario2.sh --stacks fstack --workers 1,4,8 --tests rps,bw
source "$(dirname "$0")/_common.sh"
fbench run --scenario 2 "$@"
