#!/usr/bin/env bash
# Both scenarios in one go (client machine).
source "$(dirname "$0")/_common.sh"
fbench run --scenario all "$@"
