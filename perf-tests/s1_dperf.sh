#!/bin/bash
# Сценарий 1: dperf <-> dperf, максимальная пропускная способность и оптимум по ядрам.
#   машина B:  ./s1_dperf.sh server
#   машина A:  ./s1_dperf.sh client
# Шаг = (bond, vxlan). На шаг сервер поднимается с max(S1_CORES) воркерами, а клиент
# сам перебирает число воркеров N из S1_CORES. Воркер i клиента работает с воркером i
# сервера (FDIR по IP сервера, с VXLAN — по VTEP), так что каждая точка — это N:N.
# Порядок: Enter на сервере (запуск шага) -> Enter на клиенте -> ... (по шагу за раз).
set -u
cd "$(dirname "$0")"
. ./env.sh

role=${1:-}
case $role in
    client) CPUS=$CLIENT_CPUS PCI0=$CLIENT_PCI0 PCI1=$CLIENT_PCI1 ;;
    server) CPUS=$SERVER_CPUS PCI0=$SERVER_PCI0 PCI1=$SERVER_PCI1 ;;
    *) echo "usage: $0 server|client"; exit 1 ;;
esac

die() { echo "ОШИБКА: $*" >&2; exit 1; }
NMAX=$(printf '%s\n' $S1_CORES | sort -n | tail -n1)
for c in python3 stdbuf pkill; do command -v $c >/dev/null || die "нет $c"; done
[ -x "$DPERF" ] || die "нет $DPERF — соберите: ./build_dperf.sh"
[ "$(echo $CPUS | wc -w)" -ge "$NMAX" ] || die "в списке ядер ($CPUS) меньше $NMAX"
pcis=$PCI0
case " $S1_BOND " in *" 1 "*) pcis="$PCI0 $PCI1" ;; esac
for pci in $pcis; do
    [ -e "/sys/bus/pci/devices/$pci" ] || die "PCI $pci не найден (env.sh)"
done

# Конфиг dperf: gen_conf <bond> <vxlan> <число воркеров>
gen_conf() {
    local bond=$1 vxlan=$2 n=$3 port=$PCI0 lip gw gwmac cip=$CLIENT_IP sip=$SERVER_IP payload=1460 cc
    if [ "$bond" = 1 ]; then port="bond$BOND_MODE:$BOND_POLICY($PCI0,$PCI1)"; fi
    if [ "$role" = client ]; then lip=$CLIENT_IP gw=$SERVER_IP gwmac=$SERVER_MAC
    else lip=$SERVER_IP gw=$CLIENT_IP gwmac=$CLIENT_MAC; fi
    # полный кадр 1514 Б: 1460 + 54 заголовка, с VXLAN ещё 50 Б внешних; MTU 9000 — на 7500 больше
    if [ "$vxlan" = 1 ]; then cip=$CLIENT_INNER_IP sip=$SERVER_INNER_IP payload=1410; fi
    if [ "$JUMBO" = 1 ]; then payload=$((payload + 7500)); fi

    echo "mode         $role"
    echo "cpu          $(echo $CPUS | cut -d' ' -f1-$n)"
    echo "port         $port $lip $gw $gwmac"
    if [ "$vxlan" = 1 ]; then
        # dperf раздаёт VXLAN по очередям по внешнему IP назначения: по VTEP на воркер
        if [ "$role" = client ]; then
            echo "vxlan        $VNI $CLIENT_INNER_MAC $SERVER_INNER_MAC $CLIENT_IP $n $SERVER_IP $n"
        else
            echo "vxlan        $VNI $SERVER_INNER_MAC $CLIENT_INNER_MAC $SERVER_IP $n $CLIENT_IP $n"
        fi
    fi
    echo "client       $cip 1"
    echo "server       $sip $n"
    echo "listen       80 1"
    echo "protocol     tcp"
    echo "payload_size $payload"
    echo "tx_burst     128"
    if [ "$JUMBO" = 1 ]; then echo "jumbo        9000"; fi
    if [ "$role" = client ]; then
        # замкнутый цикл: следующий запрос сразу после ответа, в полёте не больше cc пакетов
        cc=$((S1_CC_PER_CORE * n))
        echo "duration     ${S1_DURATION}s"
        echo "slow_start   10"
        echo "cc           $cc"
        echo "cps          $((cc / 5))"
        echo "keepalive    0us"
    else
        echo "duration     1d"
        echo "keepalive    1s"
    fi
}

meta() {   # <bond> <vxlan> <число воркеров>
    echo "# scenario=s1 role=$role step=bond$1-vxlan$2 bond=$1 vxlan=$2 cores=$3" \
         "jumbo=$JUMBO duration=$S1_DURATION cc_per_core=$S1_CC_PER_CORE host=$(hostname)"
}

# В терминал: строка в секунду и всё, что не статистика (ошибки, EAL)
VIEW='
/Total Numbers/ { done = 1 }
done { next }
$2 == "seconds" { s = $3; c = 0; for (i = 5; i <= NF; i++) c += $i; cpu = NF > 4 ? c / (NF - 4) : 0; next }
$2 == "pktRx" { rx = $7; tx = $9; gsub(",", "", rx); gsub(",", "", tx)
                printf "%s %4ss  rx %6.1f  tx %6.1f Gbps  cpu %3.0f%%\n", tag, s, rx / 1e9, tx / 1e9, cpu; fflush(); next }
$2 ~ /^(tcpRx|udpRx|arpRx|tosRx|kniRx|synRx|synRt|udpRt|tcpDrop|skOpen|httpGet|tcpReq|ierrors)$/ { next }
/^[0-9]+ *$/ || /^[0-9]+ -+$/ || /dperf Test Finished|^[0-9]+ (Version|License|Author):/ { next }
{ sub(/^[0-9]+ /, ""); print; fflush() }'

# dperf -> лог, каждая строка с меткой времени (по ним сводятся клиент и сервер)
run_dperf() {   # <conf> <log> <метка>
    stdbuf -oL "$DPERF" -c "$1" 2>&1 |
        while IFS= read -r l; do printf '%(%s)T %s\n' -1 "$l"; done |
        tee -a "$2" | awk -v tag="$3" "$VIEW"
}

stop_server() {
    pkill -INT -x dperf 2>/dev/null
    wait
}

SESSION=$RESULTS/s1-$(date +%Y%m%d-%H%M%S)-$role
mkdir -p "$SESSION" && cp env.sh "$SESSION/"
steps=()
for b in $S1_BOND; do for v in $S1_VXLAN; do steps+=("$b $v"); done; done
total=${#steps[@]}

if [ "$role" = server ]; then
    trap 'stop_server; exit 130' INT TERM
    read -r -p "[server] шаг 1/$total (bond${steps[0]% *}-vxlan${steps[0]#* }): Enter — запустить "
    for i in "${!steps[@]}"; do
        set -- ${steps[$i]}
        dir=$SESSION/bond$1-vxlan$2
        mkdir -p "$dir"
        gen_conf "$1" "$2" "$NMAX" > "$dir/server.conf"
        meta "$1" "$2" "$NMAX" > "$dir/server.log"
        run_dperf "$dir/server.conf" "$dir/server.log" "[srv bond$1-vxlan$2]" &
        if [ $((i + 1)) -lt "$total" ]; then
            next="остановить и запустить шаг $((i + 2))/$total (bond${steps[$((i + 1))]% *}-vxlan${steps[$((i + 1))]#* })"
        else
            next="остановить и завершить"
        fi
        echo "[server] шаг $((i + 1))/$total bond$1-vxlan$2 запущен (воркеров: $NMAX). Когда клиент закончит шаг, Enter — $next"
        read -r
        stop_server
    done
else
    for i in "${!steps[@]}"; do
        set -- ${steps[$i]}
        dir=$SESSION/bond$1-vxlan$2
        mkdir -p "$dir"
        read -r -p "[client] шаг $((i + 1))/$total bond$1-vxlan$2: запустите этот шаг на сервере, затем Enter "
        for n in $S1_CORES; do
            base=$dir/n$(printf %02d "$n")
            gen_conf "$1" "$2" "$n" > "$base.conf"
            meta "$1" "$2" "$n" > "$base.log"
            echo "--- bond$1-vxlan$2, воркеров: $n (~$((S1_DURATION + 17)) с)"
            run_dperf "$base.conf" "$base.log" "[n=$n]"
            python3 ./parse_dperf.py --brief "$base.log"
            sleep "$S1_PAUSE"
        done
        python3 ./parse_dperf.py "$dir"
    done
    python3 ./parse_dperf.py --summary "$SESSION"
fi
echo "Логи: $SESSION"
