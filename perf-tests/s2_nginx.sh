#!/bin/bash
# Сценарий 2: dperf (машина A) -> nginx на ядре и на F-Stack (машина B), без bond и VXLAN.
#   машина B:  ./s2_nginx.sh server      (от root)
#   машина A:  ./s2_nginx.sh client
# Шаг = стек nginx (S2_STACKS: kernel, fstack). Внутри шага клиент перебирает число воркеров
# nginx N (S2_CORES), а для каждого N — размеры ответа (S2_SIZES). Сервер поднимает nginx ровно
# с N воркерами на первых N ядрах SERVER_CPUS и перезапускает его со следующим N, когда клиент
# прогнал все размеры: прогоны он отсчитывает по трафику порта. dperf работает в замкнутом цикле:
# держит cc соединений, и следующий запрос уходит сразу после ответа (S2_CONN=keepalive), либо
# соединение после двух запросов закрывается и вместо него открывается новое (S2_CONN=close, CPS).
# Воркер i dperf ходит на свой адрес nginx SERVER_IP + i (FDIR, как в сценарии 1), поэтому
# у nginx S2_CLIENT_CORES адресов.
# Порядок: Enter на сервере -> Enter на клиенте, по одному разу на шаг.
set -u
cd "$(dirname "$0")"
. ./env.sh
. ./common.sh

role=${1:-}
case $role in
    client) CPUS=$CLIENT_CPUS PCI=$CLIENT_PCI0 ;;
    server) CPUS=$SERVER_CPUS PCI=$SERVER_PCI0 ;;
    *) echo "usage: $0 server|client"; exit 1 ;;
esac

K=$S2_CLIENT_CORES
NMAX=$(printf '%s\n' $S2_CORES | sort -n | tail -n1)
[ -e "/sys/bus/pci/devices/$PCI" ] || die "PCI $PCI не найден (env.sh)"
for s in $S2_SIZES; do
    [[ $s =~ ^[0-9]+[km]?:[0-9]+$ ]] || die "S2_SIZES: '$s' — нужно <размер>:<соединений>, например 64k:1000"
done
case $S2_CONN in keepalive|close) ;; *) die "S2_CONN: keepalive или close" ;; esac
case $S2_BODY in file|return) ;; *) die "S2_BODY: file или return" ;; esac
MAXCC=$(printf '%s\n' $S2_SIZES | cut -d: -f2 | sort -n | tail -n1)

first() { echo $CPUS | cut -d' ' -f1-"$1"; }       # первые N ядер роли
ipn() { echo "${1%.*}.$(( ${1##*.} + $2 ))"; }      # IP + i в последнем октете
bytes() {                                           # 64k -> 65536
    case $1 in *k) echo $(( ${1%k} * 1024 )) ;; *m) echo $(( ${1%m} * 1048576 )) ;; *) echo "$1" ;; esac
}
mode() { echo "$S2_CONN-$S2_BODY"; }                # режим точки в логах: keepalive-file, close-return, ...

# ---------- клиент ----------

# Новых соединений в секунду у dperf: keepalive — разгон до cc за 5 с, close — потолок S2_CPS
cps() { if [ "$S2_CONN" = close ]; then echo "$S2_CPS"; else echo $(( ($1 + 4) / 5 )); fi; }

# Клиентских адресов dperf: у воркера на каждый адрес 65535 портов, а сокетов нужно не меньше
# его доли cc и его доли cps за 2 с (таймаут ретрансмиссии dperf, пока сокет занят)
client_ips() {   # <соединений>
    local need=$(( $1 / K )) c=$(( $(cps "$1") / K * 2 ))
    [ "$c" -le "$need" ] || need=$c
    echo $(( need / 65000 + 1 ))
}

# Конфиг dperf: gen_conf <N воркеров nginx> <размер> <соединений>
gen_conf() {
    echo "mode         client"
    echo "cpu          $(first "$K")"
    echo "port         $CLIENT_PCI0 $CLIENT_IP $SERVER_IP $SERVER_MAC"
    echo "client       $CLIENT_IP $(client_ips "$3")"
    echo "server       $SERVER_IP $K"
    echo "listen       80 1"
    echo "protocol     http"
    echo "http_path    /n$1/$2"
    echo "tx_burst     128"
    echo "duration     ${S2_DURATION}s"
    echo "slow_start   10"
    echo "cc           $3"
    echo "cps          $(cps "$3")"
    if [ "$S2_CONN" = close ]; then
        echo "keepalive    10us 1"        # dperf: первый запрос + 1 повтор, затем закрывает сам
    else
        echo "keepalive    $S2_KEEPALIVE"
    fi
}

# ---------- сервер ----------

# Счётчики порта: пакеты rx, tx и байты rx, tx. Это счётчики vport, их ведёт карта, поэтому
# в них весь трафик порта: и ядерного стека, и F-Stack, которому DPDK отдаёт трафик мимо ядра.
nic() {
    ethtool -S "$IF" | awk '
        $1 == "rx_vport_unicast_packets:" { a = $2 }  $1 == "tx_vport_unicast_packets:" { b = $2 }
        $1 == "rx_vport_unicast_bytes:"   { c = $2 }  $1 == "tx_vport_unicast_bytes:"   { d = $2 }
        END { print a, b, c, d }'
}

gbps() {   # байт за секунду -> Gbps, один знак после запятой
    local d=$(( $1 / 12500000 ))
    printf '%5d.%d' $((d / 10)) $((d % 10))
}

cpumask() {   # <cpu> -> маска для sysfs; старше 31-го — группами по 32 бита через запятую
    local i s=
    for ((i = 0; i <= $1 / 32; i++)); do
        s=$(printf %08x $(( i == $1 / 32 ? 1 << ($1 % 32) : 0 )))${s:+,$s}
    done
    echo "$s"
}

# Адреса nginx на ядерном интерфейсе порта и очереди соединений
kernel_setup() {
    local i
    for ((i = 0; i < K; i++)); do ip addr add "$(ipn "$SERVER_IP" "$i")/24" dev "$IF"; done
    sysctl -q -w net.core.somaxconn=65535 net.ipv4.tcp_max_syn_backlog=65535
}

# Ядерный стек под N воркеров: N каналов порта, прерывание и XPS канала i — на ядре воркера i mod N.
# Вся сетевая работа ядра идёт на тех же N ядрах, что и nginx: сравнение с F-Stack на равных.
kernel_queues() {   # <ядра воркеров...>
    local cores=("$@") n=$# irq i q
    ethtool -L "$IF" combined "$n" 2>/dev/null ||
        echo "[server] $IF: не удалось задать $n каналов, сейчас их $(ethtool -l "$IF" | awk '/^Combined/ {v = $2} END {print v}')"
    while read -r irq i; do
        echo "${cores[i % n]}" > "/proc/irq/$irq/smp_affinity_list"
    done < <(awk -v p="mlx5_comp[0-9]+@pci:$PCI\$" '$NF ~ p {
                 i = $NF; sub(/@.*/, "", i); sub(/mlx5_comp/, "", i); sub(":", "", $1); print $1, i }' /proc/interrupts)
    for q in /sys/class/net/"$IF"/queues/tx-*; do
        i=${q##*-}
        cpumask "${cores[i % n]}" > "$q/xps_cpus"
    done
}

# S2_BODY=return: тела ответов прямо в конфиге, по location на размер. Только до 4000 байт:
# параметр длиннее nginx не принимает (буфер разбора конфига — 4 КБ), такие размеры идут из файлов.
return_conf() {   # <N>
    local s size b
    [ "$S2_BODY" = return ] || return 0
    for s in $S2_SIZES; do
        size=${s%%:*} b=$(bytes "${s%%:*}")
        [ "$b" -le 4000 ] || continue
        printf 'location = /n%s/%s { return 200 "%s"; }\n' "$1" "$size" \
            "$(yes 0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ | tr -d '\n' | head -c "$b")"
    done
}

# Конфиги точки из шаблонов nginx/ и запуск nginx на переднем плане (вывод в stdout.txt)
start_nginx() {   # <стек> <N> <каталог точки>
    local stack=$1 n=$2 run=$3 c aff= mask=0 vip= i bin conns nofile
    # соединений на воркер: наибольший cc из S2_SIZES с запасом в полтора раза — RSS раскладывает
    # соединения по воркерам неровно; дескрипторов — столько же и ещё на файлы
    conns=$(( MAXCC * 3 / (2 * n) + 1024 ))
    [ "$conns" -ge 65536 ] || conns=65536
    nofile=$(( conns + 4096 ))
    [ "$nofile" -le "$(cat /proc/sys/fs/nr_open)" ] || sysctl -q -w fs.nr_open="$nofile"
    mkdir -p "$run/logs"
    return_conf "$n" > "$run/return.conf"
    if [ "$stack" = kernel ]; then
        for c in $(first "$n"); do aff="$aff 1$(printf '%*s' "$c" '' | tr ' ' 0)"; done
        sed -e "s|@WORKERS@|$n|g" -e "s|@AFFINITY@|${aff# }|" -e "s|@SENDFILE@|$S2_KERNEL_SENDFILE|" \
            -e "s|@ROOT@|$WWW|g" -e "s|@CONNS@|$conns|" -e "s|@NOFILE@|$nofile|" \
            nginx/nginx-kernel.conf > "$run/nginx.conf"
        bin=$NGINX_KERNEL
    else
        for c in $(first "$n"); do mask=$(( mask | 1 << c )); done
        for ((i = 1; i < K; i++)); do vip="$vip${vip:+;}$(ipn "$SERVER_IP" "$i")"; done
        if [ -n "$vip" ]; then vip="s|@VIP@|$vip|"; else vip='/@VIP@/d'; fi
        sed -e "s|@WORKERS@|$n|g" -e "s|@RUN@|$run|g" -e "s|@ROOT@|$WWW|g" \
            -e "s|@CONNS@|$conns|" -e "s|@NOFILE@|$nofile|" \
            nginx/nginx-fstack.conf > "$run/nginx.conf"
        sed -e "s|@LCORE_MASK@|$(printf %x "$mask")|" -e "s|@PCI@|$PCI|" -e "s|@IP@|$SERVER_IP|" \
            -e "s|@BCAST@|${SERVER_IP%.*}.255|" -e "s|@GW@|$CLIENT_IP|" -e "$vip" \
            nginx/f-stack.conf > "$run/f-stack.conf"
        bin=$NGINX_FSTACK
    fi
    "$bin" -p "$run/" -c "$run/nginx.conf" -g 'daemon off;' > "$run/stdout.txt" 2>&1 &
    NGINX=$!
    sleep 2
    kill -0 "$NGINX" 2>/dev/null || fail "nginx не запустился" "$run"
}

# Загрузка ядер nginx раз в секунду: "<время> ngxcpu <% ядра 1> ... <% ядра N>".
#  kernel: /proc/stat, user+nice+system+irq+softirq. Прерывания порта на тех же ядрах,
#          так что сюда входит и сетевой стек ядра.
#  fstack: ff_top, 100% - idle: доля цикла опроса, занятая стеком и nginx. Сам опрос
#          держит ядро загруженным на 100% всегда, поэтому /proc/stat тут ничего не говорит.
sample_cpu() {   # <стек> <каталог точки> <ядра...>
    local stack=$1 run=$2
    shift 2
    if [ "$stack" = kernel ]; then
        awk -v cpus="$*" 'BEGIN {
            n = split(cpus, c, " ")
            while (1) {
                while ((getline l < "/proc/stat") > 0) {
                    split(l, f, " ")
                    if (f[1] ~ /^cpu[0-9]/) {
                        k = substr(f[1], 4); b[k] = f[2] + f[3] + f[4] + f[7] + f[8]; t[k] = b[k] + f[5] + f[6] + f[9]
                    }
                }
                close("/proc/stat")
                if (ok) {
                    "date +%s" | getline now; close("date +%s")
                    s = now " ngxcpu"
                    for (i = 1; i <= n; i++) { k = c[i]; d = t[k] - pt[k]; s = s sprintf(" %.1f", d > 0 ? 100 * (b[k] - pb[k]) / d : 0) }
                    print s; fflush()
                }
                for (k in t) { pt[k] = t[k]; pb[k] = b[k] }
                ok = 1
                system("sleep 1")
            }
        }'
    else
        stdbuf -oL "$FF_TOP" -p 0 -P $(($# - 1)) -d 1 2>&1 | tee "$run/fftop.txt" | $AWK -F'|' '
            $2 ~ /^ *[0-9]+ *$/ { v = $3; gsub(/[ %]/, "", v); s = s sprintf(" %.1f", 100 - v); next }
            $2 ~ /total/ { "date +%s" | getline now; close("date +%s"); print now " ngxcpu" s; fflush(); s = "" }'
    fi
}

start_sampler() {   # <стек> <N> <каталог точки>
    if [ "$1" = fstack ] && [ ! -x "$FF_TOP" ]; then return; fi
    sample_cpu "$1" "$3" $(first "$2") >> "$3.log" 2>/dev/null &
    SAMPLER=$!
}

stop_point() {   # остановить замер CPU и nginx текущей точки
    local i
    if [ -n "$SAMPLER" ]; then pkill -P "$SAMPLER"; kill "$SAMPLER"; fi 2>/dev/null
    if [ -n "$NGINX" ]; then
        kill "$NGINX"
        for i in $(seq 20); do kill -0 "$NGINX" || break; sleep 0.5; done
        pkill -9 -P "$NGINX"; kill -9 "$NGINX"; wait "$NGINX"
    fi 2>/dev/null
    SAMPLER= NGINX=
}

# Сбой nginx: дальше сервер и клиент разошлись бы по точкам, поэтому стоп
fail() {   # <что случилось> <каталог точки>
    stop_point
    ip addr flush dev "$IF"
    tail -n 5 "$2/stdout.txt" "$2/logs/error.log"
    die "$1 — см. $2/"
}

# Один прогон клиента: трафик порта был и упал ниже 10 тыс. пакетов/с на 3 с подряд.
# 5 минут без трафика — клиента нет, возврат 1: сервер переходит к следующему N.
# Замер CPU стартует на первом трафике: к этому времени nginx (и F-Stack) точно поднят.
wait_client_run() {   # <стек> <N> <каталог точки>
    local seen=0 idle=0 t=0 sec=0 rp tp rb tb p0 rb0 tb0 cpu
    read -r rp tp rb tb < <(nic)
    p0=$((rp + tp)) rb0=$rb tb0=$tb
    while [ "$idle" -lt 3 ]; do
        sleep 1
        kill -0 "$NGINX" 2>/dev/null || fail "nginx завершился" "$3"
        read -r rp tp rb tb < <(nic)
        if [ $((rp + tp - p0)) -ge 10000 ]; then
            [ -n "$SAMPLER" ] || start_sampler "$@"
            seen=1 idle=0 sec=$((sec + 1))
            cpu=$(tail -n 1 "$3.log" | awk '$2 == "ngxcpu" { for (i = 3; i <= NF; i++) c += $i; printf "  cpu %3.0f%%", c / (NF - 2) }')
            echo "[srv $1 n=$2] $(printf %4d $sec)s  rx $(gbps $((rb - rb0)))  tx $(gbps $((tb - tb0))) Gbps$cpu"
        elif [ "$seen" = 1 ]; then
            idle=$((idle + 1))
        else
            t=$((t + 1))
            [ "$t" -lt 300 ] || { echo "[server] 5 минут без трафика от клиента — перехожу к следующей точке"; return 1; }
        fi
        p0=$((rp + tp)) rb0=$rb tb0=$tb
    done
}

# ---------- проверки ----------

if [ "$role" = server ]; then
    IF=$(ls "/sys/bus/pci/devices/$PCI/net" 2>/dev/null | head -n1)
    [ "$(id -u)" = 0 ] || die "сервер запускается от root"
    for c in ethtool ip stdbuf pgrep pkill; do command -v $c >/dev/null || die "нет $c"; done
    [ -n "$IF" ] || die "у $PCI нет ядерного интерфейса: порт должен быть на драйвере mlx5_core"
    [ "$(echo $CPUS | wc -w)" -ge "$NMAX" ] || die "в SERVER_CPUS меньше $NMAX ядер"
    for stack in $S2_STACKS; do
        case $stack in
            kernel) [ -x "$NGINX_KERNEL" ] || die "нет $NGINX_KERNEL (NGINX_KERNEL в env.sh)" ;;
            fstack) [ -x "$NGINX_FSTACK" ] || die "нет $NGINX_FSTACK (NGINX_FSTACK в env.sh)"
                    "$NGINX_FSTACK" -V 2>&1 | grep -q -- --with-ff_module || die "$NGINX_FSTACK собран без --with-ff_module"
                    [ -x "$FF_TOP" ] || echo "ВНИМАНИЕ: нет $FF_TOP — загрузка CPU F-Stack замеряться не будет (make -C f-stack/tools)" ;;
            *) die "S2_STACKS: неизвестный стек '$stack', есть kernel и fstack" ;;
        esac
        # return — директива модуля rewrite: без него nginx с S2_BODY=return не запустится
        if [ "$S2_BODY" = return ]; then
            bin=$NGINX_KERNEL; [ "$stack" = kernel ] || bin=$NGINX_FSTACK
            "$bin" -V 2>&1 | grep -q -- --without-http_rewrite_module &&
                die "$bin собран без модуля rewrite, а S2_BODY=return отдаёт ответы директивой return"
        fi
    done
    ethtool -S "$IF" | grep -q tx_vport_unicast_packets ||
        die "ethtool -S $IF: нет счётчиков vport (tx_vport_unicast_packets) — по ним сервер видит прогоны клиента"
    pgrep -x nginx >/dev/null && die "уже работает nginx (pgrep -x nginx): остановите его — стенду нужны порт 80 и DPDK"
    if [ -e /proc/sys/net/netfilter/nf_conntrack_max ]; then
        echo "ВНИМАНИЕ: загружен nf_conntrack — ядро тратит CPU на учёт соединений, для чистого сравнения выгрузите его"
    fi
else
    for c in python3 stdbuf; do command -v $c >/dev/null || die "нет $c"; done
    [ -x "$DPERF" ] || die "нет $DPERF — соберите: ./build_dperf.sh"
    [ "$(echo $CPUS | wc -w)" -ge "$K" ] || die "в CLIENT_CPUS меньше $K ядер (S2_CLIENT_CORES)"
    # адреса dperf CLIENT_IP.. не должны заходить на адреса nginx SERVER_IP..
    c0=${CLIENT_IP##*.} s0=${SERVER_IP##*.} ips=$(client_ips "$MAXCC")
    if [ $((c0 + ips - 1)) -gt 254 ] || { [ "$c0" -le $((s0 + K - 1)) ] && [ "$s0" -le $((c0 + ips - 1)) ]; }; then
        die "dperf нужно $ips адресов от $CLIENT_IP, они заходят на адреса nginx от $SERVER_IP — уменьшите cc или S2_CPS"
    fi
fi

# ---------- прогон ----------

SESSION=$RESULTS/s2-$(date +%Y%m%d-%H%M%S)-$role
mkdir -p "$SESSION" && cp env.sh "$SESSION/"
total=$(echo $S2_STACKS | wc -w)
i=0

if [ "$role" = server ]; then
    # файлы ответов: по одному на размер, случайные данные
    WWW=$SESSION/www
    mkdir -p "$WWW"
    for s in $S2_SIZES; do head -c "$(bytes "${s%%:*}")" /dev/urandom > "$WWW/${s%%:*}"; done
    for s in $S2_SIZES; do
        if [ "$S2_BODY" = return ] && [ "$(bytes "${s%%:*}")" -gt 4000 ]; then
            echo "S2_BODY=return: ответ ${s%%:*} длиннее 4000 байт — nginx отдаёт его из файла"
        fi
    done
    SAMPLER= NGINX=
    # HUP — закрыли терминал: сам nginx на HUP лишь перечитывает конфиг, гасим его сами
    trap 'stop_point; ip addr flush dev "$IF"; exit 130' INT TERM HUP
    for stack in $S2_STACKS; do
        i=$((i + 1))
        dir=$SESSION/$stack
        mkdir -p "$dir"
        read -r -p "[server] шаг $i/$total $stack: Enter — запустить "
        # с F-Stack у ядерного интерфейса нет адресов: порт целиком у DPDK
        ip addr flush dev "$IF"
        ip link set "$IF" up
        if [ "$stack" = kernel ]; then kernel_setup; fi
        for n in $S2_CORES; do
            base=$dir/n$(printf %02d "$n")
            if [ "$stack" = kernel ]; then kernel_queues $(first "$n"); fi
            echo "# scenario=s2 role=server step=$stack stack=$stack cores=$n mode=$(mode) host=$(hostname)" > "$base.log"
            start_nginx "$stack" "$n" "$base"
            echo "[server] $stack, воркеров nginx: $n — жду прогонов клиента: $(echo $S2_SIZES | wc -w)"
            for s in $S2_SIZES; do wait_client_run "$stack" "$n" "$base" || break; done
            stop_point
        done
    done
    ip addr flush dev "$IF"
else
    for stack in $S2_STACKS; do
        i=$((i + 1))
        dir=$SESSION/$stack
        mkdir -p "$dir"
        read -r -p "[client] шаг $i/$total $stack: запустите этот шаг на сервере, затем Enter "
        for n in $S2_CORES; do
            for s in $S2_SIZES; do
                size=${s%%:*} cc=${s##*:}
                base=$dir/n$(printf %02d "$n")-$size
                gen_conf "$n" "$size" "$cc" > "$base.conf"
                echo "# scenario=s2 role=client step=$stack stack=$stack cores=$n size=$size cc=$cc mode=$(mode)" \
                     "client_cores=$K duration=$S2_DURATION host=$(hostname)" > "$base.log"
                echo "--- $stack, воркеров nginx: $n, ответ $size, соединений $cc (~$((S2_DURATION + 17)) с)"
                run_dperf "$base.conf" "$base.log" "[$stack n=$n $size]"
                python3 ./parse_dperf.py --brief "$base.log"
                sleep "$S2_PAUSE"
            done
        done
        python3 ./parse_dperf.py "$dir"
    done
    python3 ./parse_dperf.py --summary "$SESSION"
fi
echo "Логи: $SESSION"
