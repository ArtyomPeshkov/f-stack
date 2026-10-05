#!/bin/bash
# Сборка dperf (статически) на DPDK из f-stack: $DPDK_BUILD (по умолчанию f-stack/build/dpdk-build).
# Подходит и build-каталог meson, и install-prefix. Свой PKG_CONFIG_PATH имеет приоритет.
set -eu
cd "$(dirname "$0")"
. ./env.sh

if [ -z "${PKG_CONFIG_PATH:-}" ]; then
    pc=$(find "$DPDK_BUILD" -path '*meson-uninstalled/libdpdk-uninstalled.pc' 2>/dev/null | head -n1)
    [ -n "$pc" ] || pc=$(find "$DPDK_BUILD" -path '*pkgconfig/libdpdk.pc' 2>/dev/null | head -n1)
    [ -n "$pc" ] || { echo "libdpdk.pc не найден в $DPDK_BUILD (задайте DPDK_BUILD или PKG_CONFIG_PATH)"; exit 1; }
    export PKG_CONFIG_PATH=$(dirname "$pc")
fi
echo "DPDK $(pkg-config --modversion libdpdk) из $PKG_CONFIG_PATH"

make -C "$DPERF_DIR" clean
make -C "$DPERF_DIR" -j"$(nproc)"
echo "dperf $("$DPERF" -v): $DPERF"
