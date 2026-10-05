# Параметры стенда. Один и тот же файл на обеих машинах:
#   машина A — dperf client, машина B — dperf server.
# Переменные плана (S1_*, S2_*, JUMBO) можно переопределить из командной строки:
#   S1_BOND=1 S1_VXLAN=0 S1_CORES="2 4" ./s1_dperf.sh client

# --- пути: рядом лежат f-stack/ (здесь perf-tests/) и dperf/ ---
PT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FSTACK_DIR=${FSTACK_DIR:-$(dirname "$PT_DIR")}
DPDK_BUILD=${DPDK_BUILD:-$FSTACK_DIR/build/dpdk-build}
DPERF_DIR=${DPERF_DIR:-$(dirname "$FSTACK_DIR")/dperf}
DPERF=${DPERF:-$DPERF_DIR/build/dperf}
RESULTS=${RESULTS:-$PT_DIR/results}

# --- порты mlx5: полный PCI-адрес с доменом (12 символов) ---
# без bond работает только *_PCI0, bond = PCI0 + PCI1
CLIENT_PCI0=0000:3b:00.0
CLIENT_PCI1=0000:3b:00.1
SERVER_PCI0=0000:3b:00.0
SERVER_PCI1=0000:3b:00.1

# MAC порта PCI0 противоположной машины, необязательно.
# Пусто — dperf узнаёт MAC шлюза по ARP и ждёт ответа до 60 с,
# т.е. клиент нужно запустить не позже чем через минуту после сервера.
CLIENT_MAC=        # MAC клиентского PCI0 (нужен серверу)
SERVER_MAC=        # MAC серверного PCI0 (нужен клиенту)

# --- ядра под DPDK: до 12, на NUMA-узле сетевой карты, без HT-соседей ---
CLIENT_CPUS="1 2 3 4 5 6 7 8 9 10 11 12"
SERVER_CPUS="1 2 3 4 5 6 7 8 9 10 11 12"
HUGEPAGES_GB=8      # для host_tune.sh; dperf без jumbo на 12 воркеров хватает 4

# --- адреса: прямое соединение, одна L2-сеть ---
CLIENT_IP=10.0.0.1      # IP клиента; с VXLAN — первый VTEP (по VTEP на воркер: .1, .2, ...)
SERVER_IP=10.0.0.101    # первый IP сервера (по IP на воркер: .101, .102, ...); с VXLAN — первый VTEP
VNI=100
CLIENT_INNER_IP=172.16.0.1   # адреса внутри VXLAN
SERVER_INNER_IP=172.16.1.1
CLIENT_INNER_MAC=02:00:00:00:00:01
SERVER_INNER_MAC=02:00:00:00:00:02

# --- bond: 802.3ad (LACP), хеш L3+L4 ---
BOND_MODE=4
BOND_POLICY=2       # 0 = l2, 1 = l23, 2 = l34

# --- сценарий 1: dperf <-> dperf ---
S1_BOND=${S1_BOND:-"0 1"}
S1_VXLAN=${S1_VXLAN:-"0 1"}
S1_CORES=${S1_CORES:-"1 2 3 4 6 8 10 12"}  # перебор числа воркеров (N клиентских : N серверных)
S1_DURATION=${S1_DURATION:-30}            # секунд замера на точку (+ ~17 с разгон и останов)
S1_CC_PER_CORE=${S1_CC_PER_CORE:-2000}    # одновременных соединений на воркер
S1_PAUSE=${S1_PAUSE:-20}                  # пауза между точками клиента: сервер перезапускается с новым N
JUMBO=${JUMBO:-0}                         # 1 = MTU 9000 (на обеих машинах; при 8 ГБ hugepages — до 10 воркеров)

# --- сценарий 2: dperf (машина A) -> nginx на ядре и на F-Stack (машина B), без bond и VXLAN ---
# nginx работает на порту SERVER_PCI0, на первых N ядрах SERVER_CPUS, с адресами SERVER_IP,
# SERVER_IP+1, ... — по адресу на воркер dperf (FDIR, как в сценарии 1)
NGINX_KERNEL=${NGINX_KERNEL:-/usr/local/nginx/sbin/nginx}          # nginx 1.28 без F-Stack
NGINX_FSTACK=${NGINX_FSTACK:-/usr/local/nginx_fstack/sbin/nginx}   # nginx 1.28 --with-ff_module
FF_TOP=${FF_TOP:-$FSTACK_DIR/tools/sbin/top}                       # загрузка CPU F-Stack: make -C f-stack/tools
S2_STACKS=${S2_STACKS:-"kernel fstack"}               # шаги: стек nginx
S2_CORES=${S2_CORES:-"1 2 4 6 8 12"}                  # перебор числа воркеров nginx
S2_SIZES=${S2_SIZES:-"1k:2000 64k:1000 1m:200"}       # размер ответа : соединений dperf (на всех воркерах)
S2_CLIENT_CORES=${S2_CLIENT_CORES:-4}                 # воркеров dperf = адресов у nginx
S2_DURATION=${S2_DURATION:-30}                        # секунд замера на точку (+ ~17 с разгон и останов)
S2_PAUSE=${S2_PAUSE:-20}                              # пауза клиента между точками: сервер перезапускает nginx
S2_KERNEL_SENDFILE=${S2_KERNEL_SENDFILE:-on}          # off — ядро без sendfile, как F-Stack
