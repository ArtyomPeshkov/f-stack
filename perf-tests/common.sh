# Общее для s1_dperf.sh и s2_nginx.sh (подключается через source).

die() { echo "ОШИБКА: $*" >&2; exit 1; }

# mawk (awk по умолчанию в Debian/Ubuntu) читает канал блоками и выдаёт строки пачками,
# с задержкой в секунды; -W interactive — построчно. Для awk, который читает живой вывод.
AWK=awk
if awk -W version 2>/dev/null | grep -q mawk; then AWK='awk -W interactive'; fi

# В терминал: строка в секунду (с HTTP — ещё Krps и Kcps, новые соединения) и всё, что не статистика
# (ошибки, EAL). Секундный блок dperf: seconds, pktRx, ..., skOpen, httpGet (только protocol http),
# ierrors — последней.
VIEW='
/Total Numbers/ { done = 1 }
done { next }
$2 == "seconds" { s = $3; c = 0; for (i = 5; i <= NF; i++) c += $i; cpu = NF > 4 ? c / (NF - 4) : 0; k = ""; next }
$2 == "pktRx" { rx = $7; tx = $9; gsub(",", "", rx); gsub(",", "", tx); next }
$2 == "skOpen" { o = $3; gsub(",", "", o); next }
$2 == "httpGet" { k = $7; gsub(",", "", k); next }
$2 == "ierrors" { printf "%s %4ss  rx %6.1f  tx %6.1f Gbps", tag, s, rx / 1e9, tx / 1e9
                  if (k != "") printf "  %8.1f Krps  %7.1f Kcps", k / 1e3, o / 1e3
                  printf "  cpu %3.0f%%\n", cpu; fflush(); next }
$2 ~ /^(tcpRx|udpRx|arpRx|tosRx|kniRx|synRx|synRt|udpRt|tcpDrop|skOpen|tcpReq)$/ { next }
/^[0-9]+ *$/ || /^[0-9]+ -+$/ || /dperf Test Finished|^[0-9]+ (Version|License|Author):/ { next }
{ sub(/^[0-9]+ /, ""); print; fflush() }'

# dperf -> лог, каждая строка с меткой времени (по ним сводятся клиент и сервер)
run_dperf() {   # <conf> <log> <метка>
    stdbuf -oL "$DPERF" -c "$1" 2>&1 |
        while IFS= read -r l; do printf '%(%s)T %s\n' -1 "$l"; done |
        tee -a "$2" | $AWK -v tag="$3" "$VIEW"
}
