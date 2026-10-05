#!/bin/bash
# Подготовка машины к замерам. Запускать от root после каждой перезагрузки:
#   ./host_tune.sh client     # на машине A
#   ./host_tune.sh server     # на машине B
# hugepages на NUMA-узле NIC, governor performance, прерывания прочь с DPDK-ядер,
# ядерные интерфейсы mlx5 тестовых портов поднять и снять с них IP.
set -eu
cd "$(dirname "$0")"
. ./env.sh

case ${1:-} in
    client) cpus=$CLIENT_CPUS; pcis="$CLIENT_PCI0 $CLIENT_PCI1" ;;
    server) cpus=$SERVER_CPUS; pcis="$SERVER_PCI0 $SERVER_PCI1" ;;
    *) echo "usage: $0 client|server"; exit 1 ;;
esac

node=$(cat "/sys/bus/pci/devices/${pcis%% *}/numa_node")
[ "$node" -ge 0 ] || node=0

# 1. hugepages на узле NIC: 1G-страницы (иначе 2M)
hp=/sys/devices/system/node/node$node/hugepages
if [ -d $hp/hugepages-1048576kB ]; then
    echo "$HUGEPAGES_GB" > $hp/hugepages-1048576kB/nr_hugepages
    mkdir -p /mnt/huge-1G
    mountpoint -q /mnt/huge-1G || mount -t hugetlbfs -o pagesize=1G nodev /mnt/huge-1G
    echo "hugepages 1G на node$node: $(cat $hp/hugepages-1048576kB/nr_hugepages) из $HUGEPAGES_GB"
else
    echo $((HUGEPAGES_GB * 512)) > $hp/hugepages-2048kB/nr_hugepages
    mountpoint -q /dev/hugepages || mount -t hugetlbfs nodev /dev/hugepages
    echo "hugepages 2M на node$node: $(cat $hp/hugepages-2048kB/nr_hugepages)"
fi

# 2. максимальная частота CPU
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -e "$g" ] && { echo performance > "$g"; } 2>/dev/null || true
done

# 3. прерывания — только на ядра вне DPDK
systemctl stop irqbalance 2>/dev/null || true
other=
for c in $(seq 0 $(($(nproc --all) - 1))); do
    case " $cpus " in *" $c "*) ;; *) other=$other,$c ;; esac
done
other=${other#,}
for f in /proc/irq/*/smp_affinity_list; do
    { echo "$other" > "$f"; } 2>/dev/null || true
done
echo "IRQ -> CPU $other"

# 4. ядерные интерфейсы тестовых портов: up, без IP, не в Linux bond
for pci in $pcis; do
    for d in /sys/bus/pci/devices/$pci/net/*; do
        [ -e "$d" ] || continue
        ifn=$(basename "$d")
        ip addr flush dev "$ifn"
        ip link set "$ifn" up
        echo "$pci $ifn: MAC $(cat "$d/address"), speed $(cat "$d/speed" 2>/dev/null) Mb/s"
        [ -e "$d/master" ] && echo "ВНИМАНИЕ: $ifn в $(basename "$(readlink "$d/master")") — уберите его из Linux bond"
    done
done

# 5. проверки
for c in $cpus; do
    [ -e /sys/devices/system/cpu/cpu$c/node$node ] || echo "ВНИМАНИЕ: CPU $c не на node$node (узел NIC)"
done
grep -qw isolcpus /proc/cmdline || echo "Совет: изолируйте DPDK-ядра (isolcpus, nohz_full, rcu_nocbs) — см. README"
grep -qw 'iommu=pt' /proc/cmdline || echo "Совет: добавьте iommu=pt в параметры ядра — см. README"
exit 0
