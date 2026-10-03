#!/usr/bin/env bash
# Server side: starts the agent that executes the orchestrator's commands
# (dperf server, nginx kernel / F-Stack, network setup, CPU sampling). Ctrl-C stops
# it and restores the machine (drivers, sysctl, irqbalance).
source "$(dirname "$0")/_common.sh"
fbench agent "$@"
