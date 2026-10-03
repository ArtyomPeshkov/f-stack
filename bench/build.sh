#!/usr/bin/env bash
# Builds dperf against the DPDK of F-Stack (dpdk_build in bench.conf), libfstack,
# F-Stack tools and nginx in two flavours (F-Stack / kernel) of the same source.
# Run on BOTH machines. "./build.sh --force" rebuilds everything.
source "$(dirname "$0")/_common.sh"
fbench build "$@"
