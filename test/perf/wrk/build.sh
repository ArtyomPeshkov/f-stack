#!/bin/sh
# Build wrk for F-Stack load generation.
#
# wrk vendors its own LuaJIT and OpenSSL under deps/, so the only host
# requirements are a C toolchain plus unzip/tar/perl. The resulting binary is
# self-contained apart from libc/libm/libgcc and can be copied to any load
# generator host of the same architecture.
#
# Verified: wrk 4.2.0 [epoll], gcc 13, Ubuntu 24.04.
#
# Usage: ./build.sh [install-dir]      (default: ./wrk-build)

set -e

DEST=${1:-$(pwd)/wrk-build}
WRK_REPO=https://github.com/wg/wrk.git
WRK_TAG=4.2.0

for tool in gcc make unzip tar perl git; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found. On Ubuntu/Debian:" >&2
        echo "  apt-get install -y build-essential unzip perl git" >&2
        echo "On CentOS/RHEL:" >&2
        echo "  yum install -y gcc make unzip perl git" >&2
        exit 1
    }
done

mkdir -p "$DEST"
cd "$DEST"

if [ ! -d wrk ]; then
    git clone --depth 1 --branch "$WRK_TAG" "$WRK_REPO" wrk 2>/dev/null \
        || git clone --depth 1 "$WRK_REPO" wrk
fi

cd wrk
make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

echo
echo "built: $(pwd)/wrk"
./wrk --version 2>&1 | head -1
echo
echo "Copy this binary to the load generator host. It links only libc/libm/libgcc:"
ldd ./wrk
