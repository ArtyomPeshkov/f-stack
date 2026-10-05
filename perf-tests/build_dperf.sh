#!/bin/bash
# Сборка dperf (статически) на DPDK из f-stack: $DPDK_BUILD (по умолчанию f-stack/build/dpdk-build).
# Подходит и build-каталог meson, и install-prefix.
set -eu
cd "$(dirname "$0")"
. ./env.sh

if [ ! -f "$DPERF_DIR/Makefile" ]; then
    echo "В $DPERF_DIR нет Makefile dperf."
    alt=$(grep -l '^APP=dperf' "$DPERF_DIR"/*/Makefile 2>/dev/null | head -n1)
    if [ -n "$alt" ]; then echo "Похоже, исходники в $(dirname "$alt"): пропишите DPERF_DIR=$(dirname "$alt") в env.sh"; fi
    exit 1
fi

# DPDK из f-stack — первым в PKG_CONFIG_PATH, даже если тот уже задан (например, на системный DPDK)
pc=$(find "$DPDK_BUILD" -path '*meson-uninstalled/libdpdk-uninstalled.pc' 2>/dev/null | head -n1)
[ -n "$pc" ] || pc=$(find "$DPDK_BUILD" -path '*pkgconfig/libdpdk.pc' 2>/dev/null | head -n1)
if [ -n "$pc" ]; then
    export PKG_CONFIG_PATH=$(dirname "$pc")${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
else
    echo "ВНИМАНИЕ: libdpdk.pc не найден в $DPDK_BUILD, ищу DPDK по PKG_CONFIG_PATH=${PKG_CONFIG_PATH:-}"
fi
pkg-config --exists libdpdk || { echo "DPDK не найден: задайте DPDK_BUILD в env.sh"; exit 1; }
echo "DPDK $(pkg-config --modversion libdpdk): $(pkg-config --variable=pcfiledir libdpdk)"

# Необязательной библиотеки pdump в сборке DPDK под f-stack может не быть:
# тогда dperf собирается с заглушкой (теряется только захват пакетов через dpdk-pdump)
pdump=
for d in $(pkg-config --cflags-only-I libdpdk); do
    if [ -e "${d#-I}/rte_pdump.h" ]; then pdump=1; fi
done
if [ -z "$pdump" ]; then
    echo "В DPDK нет библиотеки pdump: dperf собирается с заглушкой compat/pdump"
    export CFLAGS="${CFLAGS:-} -I$PT_DIR/compat/pdump"
fi

# RTE_SDK переключает Makefile dperf на сборку под старый DPDK (до 20.11)
unset RTE_SDK RTE_TARGET
rm -rf "$DPERF_DIR/build"
make -C "$DPERF_DIR" -j"$(nproc)"
echo "dperf $("$DPERF" -v): $DPERF"
