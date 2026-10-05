# Параметры стенда. Один и тот же файл на обеих машинах:
#   машина A — dperf client, машина B — dperf server.
# Переменные плана (S1_*, JUMBO) можно переопределить из командной строки:
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
