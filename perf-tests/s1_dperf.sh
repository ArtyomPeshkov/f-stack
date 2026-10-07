#!/bin/bash
# Сценарий 1: dperf <-> dperf, максимальная пропускная способность и оптимум по ядрам.
#   машина B:  ./s1_dperf.sh server
#   машина A:  ./s1_dperf.sh client
# Шаг = (bond, vxlan, направление). Внутри шага обе стороны перебирают число воркеров N из S1_CORES:
# машина A запускает точки подряд с паузой S1_PAUSE, машина B поднимает dperf ровно с N воркерами
# и перезапускает его со следующим N, когда трафик пропадает. Воркер i клиента
# работает с воркером i сервера (FDIR по IP сервера, с VXLAN — по VTEP): каждая точка — N:N.
# Направление (S1_DIR): bi — запрос и ответ по полному кадру, трафик в обе стороны;
# b2a и a2b — поток в одну сторону. Поток в dperf отдаёт только сервер, ответами HTTP,
# поэтому в шаге a2b dperf server работает на машине A, а dperf client — на B.
# Порядок: Enter на машине B -> Enter на машине A, по одному разу на шаг.
set -u
cd "$(dirname "$0")"
. ./env.sh
. ./common.sh

role=${1:-}
case $role in
    client) CPUS=$CLIENT_CPUS PCI0=$CLIENT_PCI0 PCI1=$CLIENT_PCI1 ;;
    server) CPUS=$SERVER_CPUS PCI0=$SERVER_PCI0 PCI1=$SERVER_PCI1 ;;
    *) echo "usage: $0 server|client"; exit 1 ;;
esac

NMAX=$(printf '%s\n' $S1_CORES | sort -n | tail -n1)
for c in python3 stdbuf pkill pgrep; do command -v $c >/dev/null || die "нет $c"; done
[ -x "$DPERF" ] || die "нет $DPERF — соберите: ./build_dperf.sh"
[ "$(echo $CPUS | wc -w)" -ge "$NMAX" ] || die "в списке ядер ($CPUS) меньше $NMAX"
pcis=$PCI0
case " $S1_BOND " in *" 1 "*) pcis="$PCI0 $PCI1" ;; esac
for pci in $pcis; do
    [ -e "/sys/bus/pci/devices/$pci" ] || die "PCI $pci не найден (env.sh)"
done
for d in $S1_DIR; do
    case $d in bi|b2a|a2b) ;; *) die "S1_DIR: bi, b2a или a2b, а не $d" ;; esac
done

step() {   # <bond> <vxlan> <направление> -> имя шага: bond0-vxlan1, bond0-vxlan1-b2a
    local s=bond$1-vxlan$2
    [ "$3" = bi ] || s=$s-$3
    echo "$s"
}

# Режим dperf на этой машине: поток отдаёт сервер, поэтому в шаге a2b машины меняются режимами
dperf_mode() {   # <направление>
    case $1-$role in a2b-client) echo server ;; a2b-server) echo client ;; *) echo "$role" ;; esac
}

cc_per_core() {   # <направление>
    if [ "$1" = bi ]; then echo "$S1_CC_PER_CORE"; else echo "$S1_UNI_CC_PER_CORE"; fi
}

# Конфиг dperf: gen_conf <bond> <vxlan> <направление> <число воркеров>
gen_conf() {
    local bond=$1 vxlan=$2 dir=$3 n=$4 mode port=$PCI0 lip gw gwmac cip=$CLIENT_IP sip=$SERVER_IP payload=1460 cc
    mode=$(dperf_mode "$dir")
    if [ "$bond" = 1 ]; then port="bond$BOND_MODE:$BOND_POLICY($PCI0,$PCI1)"; fi
    if [ "$mode" = client ]; then lip=$CLIENT_IP gw=$SERVER_IP; else lip=$SERVER_IP gw=$CLIENT_IP; fi
    if [ "$role" = client ]; then gwmac=$SERVER_MAC; else gwmac=$CLIENT_MAC; fi    # MAC другой машины
    # полный кадр 1514 Б: 1460 + 54 заголовка, с VXLAN ещё 50 Б внешних; MTU 9000 — на 7500 больше
    if [ "$vxlan" = 1 ]; then cip=$CLIENT_INNER_IP sip=$SERVER_INNER_IP payload=1410; fi
    if [ "$JUMBO" = 1 ]; then payload=$((payload + 7500)); fi

    echo "mode         $mode"
    echo "cpu          $(echo $CPUS | cut -d' ' -f1-$n)"
    echo "port         $port $lip $gw $gwmac"
    if [ "$vxlan" = 1 ]; then
        # dperf раздаёт VXLAN по очередям по внешнему IP назначения: по VTEP на воркер
        if [ "$mode" = client ]; then
            echo "vxlan        $VNI $CLIENT_INNER_MAC $SERVER_INNER_MAC $CLIENT_IP $n $SERVER_IP $n"
        else
            echo "vxlan        $VNI $SERVER_INNER_MAC $CLIENT_INNER_MAC $SERVER_IP $n $CLIENT_IP $n"
        fi
    fi
    echo "client       $cip 1"
    echo "server       $sip $n"
    echo "listen       80 1"
    if [ "$dir" = bi ]; then
        echo "protocol     tcp"
        echo "payload_size $payload"
    else
        # поток — ответы HTTP по S1_UNI_SIZE: сервер режет их на сегменты по mss (полные кадры)
        # и держит в полёте до 16 кадров на соединение; клиент шлёт только GET и ACK
        echo "protocol     http"
        echo "mss          $payload"
        if [ "$mode" = server ]; then
            echo "payload_size $S1_UNI_SIZE"
            echo "send_window  16"
        fi
    fi
    echo "tx_burst     128"
    if [ "$JUMBO" = 1 ]; then echo "jumbo        9000"; fi
    # dperf на машине A работает S1_DURATION (+10 с разгона), на машине B — пока его не остановит скрипт
    if [ "$mode" = client ]; then
        # замкнутый цикл: следующий запрос сразу после ответа; в полёте на воркер не больше
        # cc кадров (bi) или cc x 16 (b2a, a2b) — меньше RX-кольца dperf (4096)
        cc=$(($(cc_per_core "$dir") * n))
        if [ "$role" = client ]; then echo "duration     ${S1_DURATION}s"; else echo "duration     1d"; fi
        echo "slow_start   10"
        echo "cc           $cc"
        echo "cps          $((cc / 5))"
        echo "keepalive    0us"
    else
        if [ "$role" = client ]; then echo "duration     $((S1_DURATION + 10))s"; else echo "duration     1d"; fi
        echo "keepalive    1s"
    fi
}

meta() {   # <bond> <vxlan> <направление> <число воркеров>
    echo "# scenario=s1 role=$role dperf=$(dperf_mode "$3") step=$(step "$1" "$2" "$3") bond=$1 vxlan=$2 dir=$3" \
         "cores=$4 jumbo=$JUMBO duration=$S1_DURATION cc_per_core=$(cc_per_core "$3") host=$(hostname)"
}

stop_server() {
    pkill -INT -x dperf 2>/dev/null
    wait
}

# Машина B ждёт конца прогона машины A: трафик (rx + tx) был и упал ниже 1 Gbps на 3 с подряд.
# Если трафика нет 5 минут (машина A не стартовала), B переходит к следующей точке.
wait_client_run() {   # <лог dperf>
    local seen=0 idle=0 t=0 g
    while [ "$idle" -lt 3 ] && [ "$t" -lt 300 ]; do
        sleep 1
        pgrep -x dperf >/dev/null || { echo "[server] dperf завершился — см. $1"; return; }
        g=$(tail -n 30 "$1" | awk '$2 == "pktRx" {rx = $7; tx = $9}
                                    END {gsub(",", "", rx); gsub(",", "", tx); printf "%d", (rx + tx) / 1e9}')
        if [ "${g:-0}" -ge 1 ]; then seen=1 idle=0
        elif [ "$seen" = 1 ]; then idle=$((idle + 1))
        else t=$((t + 1)); fi
    done
    [ "$seen" = 1 ] || echo "[server] 5 минут без трафика — перехожу к следующей точке"
}

SESSION=$RESULTS/s1-$(date +%Y%m%d-%H%M%S)-$role
mkdir -p "$SESSION" && cp env.sh "$SESSION/"
steps=()
for b in $S1_BOND; do for v in $S1_VXLAN; do for d in $S1_DIR; do steps+=("$b $v $d"); done; done; done
total=${#steps[@]}

if [ "$role" = server ]; then
    trap 'stop_server; exit 130' INT TERM
    for i in "${!steps[@]}"; do
        set -- ${steps[$i]}
        name=$(step "$@")
        out=$SESSION/$name
        mkdir -p "$out"
        read -r -p "[server] шаг $((i + 1))/$total $name: Enter — запустить "
        # на каждую точку ровно N воркеров: простаивающие воркеры dperf тоже крутят опрос
        # на 100% и отнимают ядра (HT, планировщик хоста) у активных
        for n in $S1_CORES; do
            base=$out/n$(printf %02d "$n")
            gen_conf "$@" "$n" > "$base.conf"
            meta "$@" "$n" > "$base.log"
            echo "[server] $name, воркеров: $n — жду прогона клиента"
            run_dperf "$base.conf" "$base.log" "[srv n=$n]" &
            wait_client_run "$base.log"
            stop_server
        done
    done
else
    for i in "${!steps[@]}"; do
        set -- ${steps[$i]}
        name=$(step "$@")
        out=$SESSION/$name
        mkdir -p "$out"
        read -r -p "[client] шаг $((i + 1))/$total $name: запустите этот шаг на сервере, затем Enter "
        for n in $S1_CORES; do
            base=$out/n$(printf %02d "$n")
            gen_conf "$@" "$n" > "$base.conf"
            meta "$@" "$n" > "$base.log"
            echo "--- $name, воркеров: $n (~$((S1_DURATION + 17)) с)"
            run_dperf "$base.conf" "$base.log" "[n=$n]"
            python3 ./parse_dperf.py --brief "$base.log"
            sleep "$S1_PAUSE"
        done
        python3 ./parse_dperf.py "$out"
    done
    python3 ./parse_dperf.py --summary "$SESSION"
fi
echo "Логи: $SESSION"
