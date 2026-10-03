# shellcheck shell=bash
# Common helper for the fbench wrappers: run fbench.py as root from this directory.
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BENCH_DIR" || exit 1
if [ ! -f bench.conf ] && [ -z "${FBENCH_CONF:-}" ]; then
    echo "bench.conf not found: cp bench.conf.example bench.conf and fill it in (same file on both machines)" >&2
    exit 1
fi
CONF="${FBENCH_CONF:-$BENCH_DIR/bench.conf}"
fbench() {
    if [ "$(id -u)" -ne 0 ]; then
        exec sudo -E python3 "$BENCH_DIR/fbench.py" -c "$CONF" "$@"
    fi
    exec python3 "$BENCH_DIR/fbench.py" -c "$CONF" "$@"
}
