# fbench — стенд максимальной производительности dperf / F-Stack / nginx

Две машины, «одна кнопка»:

* **Сценарий 1** — `dperf client <-> dperf server`: максимум, который выжимает dperf
  (TCP-throughput, пакетная скорость, CPS) × {single, bond} × {без VXLAN, VXLAN} ×
  перебор числа ядер → оптимум по CPU.
* **Сценарий 2** — `dperf client -> nginx`: один и тот же nginx 1.28.0 из `app/` поверх
  **ядра Linux** и поверх **F-Stack** × {single, bond} × {без VXLAN, VXLAN} × перебор числа
  воркеров → сравнение throughput и затраченного CPU, вердикт по каждому тесту.

dperf собирается против DPDK из F-Stack (`f-stack/build/dpdk-build`), nginx — в двух
вариантах из одного исходника (`--with-ff_module` и без него).

## Состав

```
bench/
  bench.conf.example   конфиг (один и тот же файл на обеих машинах)
  build.sh             сборка: dperf (против DPDK из F-Stack), libfstack, ff_* tools, nginx x2
  check.sh             проверка машины перед запуском (client|server)
  agent.sh             агент на машине B (сервер): выполняет команды оркестратора
  run_scenario1.sh     сценарий 1 целиком (запуск на машине A)
  run_scenario2.sh     сценарий 2 целиком (запуск на машине A)
  run_all.sh           оба сценария подряд
  restore.sh           аварийная очистка: убить dperf/nginx, вернуть порты драйверу ядра
  fbench.py, fbench/   оркестратор (python3 >= 3.6, только stdlib)
  patches/             исправление dperf (build.sh накладывает его сам)
```

## Схема стенда

```
        management-сеть (ssh/агент, порт 9777)
  +-----------------------------+                 +-----------------------------+
  | машина A  "client"          |                 | машина B  "server"          |
  |  run_scenario*.sh           |  <---- TCP ---> |  agent.sh                   |
  |  dperf client (DPDK)        |                 |  dperf server   (сцен. 1)   |
  |                             |                 |  nginx kernel   (сцен. 2)   |
  |  port0 ===================== тестовые порты ===== port0  nginx F-Stack (2)   |
  |  port1 ===================== (bond / dual) ======= port1                     |
  +-----------------------------+                 +-----------------------------+
```

* `single` — один порт; `bond` — два порта в bond (802.3ad/LACP или balance-xor, хеш L3+L4);
  `dual` — два независимых порта (опционально: максимум без накладных расходов bond).
* Порты соединены напрямую или через коммутатор (для `bond_mode=4` на коммутаторе нужен LACP).
* Агент и оркестратор общаются по management-сети (не по тестовым портам): тестовые порты
  на время прогона отдаются DPDK.

## Подготовка (один раз)

1. **Обе машины**: рядом лежат `<root>/f-stack` (этот репозиторий) и `<root>/dperf`;
   DPDK собран в `f-stack/build/dpdk-build` (meson build-dir или install-prefix — подходят оба).
   Если ещё нет: `cd f-stack/dpdk && meson setup ../build/dpdk-build && ninja -C ../build/dpdk-build`.

2. **Конфиг** — один файл на обе машины:
   ```
   cd f-stack/bench
   cp bench.conf.example bench.conf
   vi bench.conf            # PCI-адреса, ядра, server_addr (management IP машины B), token
   scp bench.conf machineB:<root>/f-stack/bench/
   ```
   Что заполнить обязательно: `[control] server_addr`, `token`; `[client]`/`[server]`
   `pci` (полный вид `0000:3b:00.0`, 1-й порт — single, 1-й+2-й — bond/dual), `cores`
   (до 12 ядер на NUMA-ноде NIC, без ядра 0 и без HT-соседей), `hugepages_gb`,
   `dpdk_driver` (`vfio-pci`; для Mellanox — `none`: mlx5 работает поверх драйвера ядра).
   Оркестратор сверяет хеш общих секций и откажется работать с разными конфигами.

3. **Сборка — на обеих машинах**: `./build.sh`
   * если `dpdk_build` — meson build-dir, сначала инкрементально пересобирает DPDK
     (`ninja -C dpdk_build`): в этой ветке исправлен bonding PMD в `f-stack/dpdk`, без
     пересборки DPDK bond в F-Stack с несколькими воркерами работать не будет;
   * находит `libdpdk` в `dpdk_build` (`meson-uninstalled/` или `lib*/pkgconfig`) и собирает
     dperf статически против **этого** DPDK (`dperf/build/dperf`); исходники dperf
     автоматически исправляются (ошибка контрольной суммы inner TCP в VXLAN, см. ниже);
   * собирает `libfstack.a`, `f-stack/tools/sbin/{ifconfig,route,arp,top,...}`;
   * собирает nginx из `app/nginx-1.28.0` дважды: `work/install/nginx-fstack`
     (`--with-ff_module`) и `work/install/nginx-kernel` (без него: `NGX_HAVE_FSTACK` не
     определён, код F-Stack не используется — это обычный nginx той же версии).
   Повторный `./build.sh` пересобирает только то, что старше DPDK/`libfstack.a`/исходников
   `lib/`. Это важно: процессы F-Stack и утилиты `ff_*` — primary/secondary-процессы одного
   DPDK, бинарники из разных сборок DPDK несовместимы (`ff_top` падает при подключении).
   `check.sh` предупреждает об устаревших бинарниках.
   Готовые бинарники можно указать в `[paths]` (`dperf_bin`, `nginx_fstack`, `nginx_kernel`).
   `./build.sh --force` — пересобрать всё.

4. **Проверка**: `./check.sh client` на A, `./check.sh server` на B — бинарники, DPDK,
   драйверы портов, NUMA, hugepages, isolcpus/nohz_full и пр.

5. **Рекомендуется (перезагрузка/BIOS)** — см. раздел «Оптимизации», пункт «вручную».

## Запуск «одной кнопкой»

На машине **B** (сервер):
```
./agent.sh          # держать запущенным; Ctrl-C — остановка с восстановлением машины
```
На машине **A** (клиент):
```
./run_scenario1.sh  # dperf <-> dperf
./run_scenario2.sh  # dperf -> nginx (kernel vs F-Stack)
./run_all.sh        # оба
```
Если на A настроен ssh по ключу до B (root), впишите `[control] ssh = root@<B>` — тогда агент
стартует сам и запуск становится буквально одной командой на A.

Полезные опции (передаются в `fbench.py run`):
```
--topologies single,bond,dual   --vxlan off,on
--tests tps,pps,cps,bw          (сценарий 1)    --tests rps,cps,bw (сценарий 2)
--cores 1,2,4,8                 (сценарий 1: ядер на сторону)
--workers 1,2,4,8               (сценарий 2: воркеров nginx)
--stacks kernel,fstack          --duration 20  --ramp 10  --quick
--name <метка>                  --keep-bindings
```
`python3 fbench.py plan -s all` — показать матрицу и оценку времени, ничего не запуская;
`python3 fbench.py gen -s all` — сгенерировать в `work/generated/` **все** конфиги (dperf
клиента/сервера, `f-stack.conf`, `nginx.conf`) и проверить каждый конфиг dperf через `dperf -t`.

Полная матрица по умолчанию (single+bond, VXLAN off/on) — порядка 4–5 часов; ориентируйтесь
по `plan` и сужайте фильтрами. Прерванный прогон: Ctrl-C на A (всё будет остановлено и
восстановлено на обеих машинах), при сбое — `./restore.sh client|server`.

## Что и как меряется

### Сценарий 1: dperf <-> dperf

| тест | что | нагрузка | метрика |
|---|---|---|---|
| `tps` | максимальный TCP-throughput | keep-alive, запрос и ответ по 1400 B (максимум без jumbo), `cc = 4000 × ядер`, `keepalive 1ms` | Gbps RX+TX на клиенте |
| `pps` | пакетная скорость | UDP flood, кадр 64 B, `cc = 20000 × ядер` | Mpps, принятые сервером |
| `cps` | соединения в секунду | короткие TCP-соединения, 1 B | conn/s (пик 5 с) |
| `bw` (опц.) | большие ответы сервера | ответ 64 KB, `send_window 16` | Gbps RX |

* Для каждого (топология, VXLAN, тест) перебирается число ядер `n` (одинаково на обеих
  сторонах). **Оптимум** = минимальное `n`, при котором результат ≥ 95% максимума кривой
  (`optimum_tolerance`). В отчёте — максимум, `n` оптимума и производительность на ядро.
* Распределение по ядрам — FDIR (rte_flow): по одному server-IP на ядро, без межъядерного
  обмена. Если NIC не умеет такие правила, стенд сам переключается на `rss` (для VXLAN
  с несколькими ядрами FDIR обязателен — ограничение dperf).
* VXLAN: на каждое ядро свой VTEP (`x.x.1.N` сервер, `x.x.2.N` клиент), FDIR по outer dst IP;
  inner MSS = 1410.
* `cps` ищется в 2 прохода: разгон до цели → найденный пик × 1.25 → итог. Цель на ядро
  (`cps_per_core`) автоматически урезается под бюджет hugepages (сокеты dperf живут в hugepages:
  80 B на сокет, сокетов нужно `cps × RTO(2 c)`).
* CPU: dperf сам считает загрузку воркеров (`cpuUsage` = доля полезной работы цикла поллинга);
  она есть и в отчёте для клиента и сервера.

### Сценарий 2: dperf -> nginx (kernel vs F-Stack)

| тест | что | метрика |
|---|---|---|
| `rps` | keep-alive, маленький ответ `return 200 "<600 B>"` (методика F-Stack) | запросов/с |
| `cps` | короткие соединения (1 запрос на соединение), тот же ответ | соединений/с (пик 5 с, 2 прохода) |
| `bw` | keep-alive, статический файл (`bw_size`, по умолчанию 1 MB) из tmpfs | Gbps RX |

* Перебор числа воркеров nginx `W` (= ядер сервера). dperf-клиент всегда использует все свои
  ядра (`client_cores`), чтобы узким местом был сервер; если клиент упёрся в CPU (≥ 90%),
  точка помечается флагом `client-bound`.
* **Kernel**: ядро получает ровно `W` ядер: каналов NIC = `W`, IRQ очереди q → ядро q, XPS,
  RPS выключен, irqbalance остановлен, воркеры nginx прибиты к тем же ядрам
  (`worker_cpu_affinity`), `reuseport`, `sendfile`, sysctl (backlog, буферы, tw).
  CPU = занятость **всей** машины за окно замера (user+sys+irq+softirq, в ядрах).
* **F-Stack**: `lcore_mask` на `W` ядер, RSS на `W` очередей, `tso=1`, `pkt_tx_delay=0`,
  буферы сокетов 256 KB, `sendfile off` (F-Stack не может sendfile файл хоста), kqueue.
  CPU: «alloc» = `W` ядер, занятых поллингом на 100% (так это видит ОС), «busy» = полезная
  работа по `ff_top` (sys+usr). В отчёте приводятся обе цифры.
* VXLAN: kernel — устройство `vxlan` поверх порта/бонда; F-Stack — FreeBSD `vxlan(4)` в
  **каждом** процессе F-Stack, настраивается через `ff_ifconfig/ff_route/ff_arp`.
* Вердикт по каждому тесту: максимум каждого стека и при каком `W`, отношение
  F-Stack/kernel, оптимальное число воркеров для каждого стека, и сколько воркеров нужно
  F-Stack, чтобы догнать максимум ядра.

### Окно замера

Каждая точка: `wait 3 c` → разгон `ramp` (`slow_start` dperf) → `duration` секунд стационара;
метрики усредняются по `[ramp + settle, ramp + duration)`. CPU сервера снимается агентом
синхронно (старт/стоп окна приходят от клиента по секундам статистики dperf).
Флаги в отчёте: `client-bound`, `server-busy`, `errors` (ошибки/ретрансмиты/imissed > 0.1%),
`unsaturated` (для `cps` цель не достигла насыщения — поднимите `cps_per_*`).

Gbps в отчёте — как считает dperf: L2-кадры без преамбулы/IFG/FCS. На 100GbE при кадре
1454 B «линейная скорость» это ≈ 98.4 Gbps в каждую сторону.

## Результаты

`results/<дата>-<метка>-s<сценарий>/`:
* `report.md` — сводка и таблицы (оптимумы, сравнения, вердикты), описание машин и
  предупреждения проверок;
* `summary.csv` — плоская таблица всех точек;
* `results.jsonl` — сырые записи (все счётчики окна, CPU, параметры);
* `points/<точка>/` — конфиги dperf обеих сторон, логи dperf, `nginx.conf`, `f-stack.conf`,
  хвост error.log, диагностика сервера при ошибке;
* `run.json`, `run.log` — параметры прогона и полный лог.

Перестроить отчёт: `python3 fbench.py report results/<каталог>`.

## Оптимизации

### Применяются автоматически
* hugepages на NUMA-ноде NIC (1G, остаток 2M), монтирование hugetlbfs;
* governor `performance` и PM QoS `cpu_dma_latency=0` (нет глубоких C-states) на время прогона;
* dperf: `tx_burst 128`, `launch_num 10`, 4096 дескрипторов (по умолчанию в dperf),
  FDIR-распределение (каждое ядро обслуживает свой server-IP — нет общих структур),
  closed-loop keep-alive с большим `cc`, payload 1400 (максимальный кадр без jumbo),
  число client IP рассчитывается под `cps × RTO` и бюджет hugepages;
* bond: хеш L3+L4 (поток → слейв), LACP fast rate; dperf шлёт LLDP раз в 100 мс, чтобы
  LACP в DPDK bond mode 4 не «засыпал»; F-Stack использует выделенные очереди LACP, если NIC
  умеет rte_flow по ethertype, иначе (после исправления в этой ветке) сам выталкивает LACPDU
  пустым `tx_burst` раз в 10 мс;
* kernel: каналы = воркерам, IRQ/XPS 1:1, RPS off, irqbalance off, `reuseport`,
  `worker_cpu_affinity`, sendfile+tcp_nopush, open_file_cache, большие backlog/буферы,
  RSS по портам UDP (энтропия VXLAN в outer UDP src port);
* F-Stack: TSO (`fstack_tso`), `pkt_tx_delay=0`, `idle_sleep=0`, буферы TCP 256 KB, `allow`
  только нужных портов, vip-адреса под FDIR клиента;
* nginx: `access_log off`, `keepalive_requests 1e9`, `multi_accept`, `accept_mutex off`.

### Вручную (сильно влияет на результат)
* **Cmdline ядра** (обе машины): `isolcpus=<ядра DPDK> nohz_full=<…> rcu_nocbs=<…>
  default_hugepagesz=1G hugepagesz=1G hugepages=8 intel_iommu=on iommu=pt` (AMD: `amd_iommu=on`).
  Для сценария 2 (kernel) isolcpus не мешает: воркеры и IRQ прибиваются явно.
* **BIOS**: профиль Performance, C-states/C1E off, Turbo — on (максимум) или off
  (стабильность замеров), SNC/NPS=1, HT — on, но берите по одному потоку на физическое ядро
  (`check.sh` предупреждает о HT-соседях).
* **NIC**: прошивка/драйвер свежие; NIC в слоте PCIe Gen3 x16/Gen4 для 100G; ядра — на
  NUMA-ноде NIC; Intel E810 — загруженный DDP-пакет (ice.pkg) для FDIR/RSS по туннелям;
  Mellanox — `dpdk_driver = none` (bifurcated), при желании CQE-compression.
* **Без conntrack** на сервере для kernel-тестов (`check.sh server` предупредит):
  conntrack заметно режет CPS ядра.
* Jumbo (только сценарий 1): `jumbo = off on` даёт кривую для MTU 9000; dperf держит пул
  ~750 MB hugepages на ядро, поэтому при 8 GB jumbo реально до ~8 ядер на сторону.

### Что ожидать (порядки величин)
* Линейная скорость: 100GbE — 8.13 Mpps при кадре 1518 B, 148.8 Mpps при 64 B;
  25GbE — 2.03/37.2 Mpps. dperf на одном современном ядре держит ~15–17 Mpps,
  ~4 M CPS и почти 100 Gbps при 1400 B — поэтому в `tps` на 100G насыщение наступает
  на 1–2 ядрах на сторону, на bond 2×100G — на 2–4; `pps` и `cps` масштабируются почти
  линейно до предела NIC.
* VXLAN: +50 B на пакет (inner MSS 1410, ~3% полезной полосы) и программная
  инкапсуляция в dperf — кривая сдвигается вправо (нужно больше ядер для того же результата).
* Kernel vs F-Stack: на маленьких ответах (`rps`, `cps`) F-Stack обычно выигрывает в разы
  на ядро; на больших файлах (`bw`) ядро с TSO+sendfile конкурентно по Gbps, но тратит
  меньше «выделенных» ядер (F-Stack поллит 100%). С VXLAN у F-Stack нет inner-offload
  (checksum/TSO внутреннего пакета в софте), у ядра — есть tunnel offload NIC.

## Ограничения и особенности
* **VXLAN в сценарии 2: 1 ядро dperf-клиента на порт.** dperf раскладывает очереди по
  VTEP (FDIR по outer dst IP), а VXLAN-устройство ядра и FreeBSD `vxlan(4)` отвечают на
  единственный удалённый VTEP — несколько ядер клиента не получат свои ответы. Сервер при
  этом использует все `W` ядер (RSS по outer UDP-порту, который dperf варьирует по потоку).
  Если клиент упирается (`client-bound`), это видно в отчёте.
* Сценарий 1 c VXLAN на нескольких ядрах требует FDIR на NIC (rss + vxlan dperf не умеет).
* bond mode 4 требует LACP на другой стороне (на коммутаторе — тоже). Без LACP используйте
  `bond_mode = 2` (balance-xor) — работает напрямую без согласования.
* Jumbo в сценарии 2 не тестируется (в F-Stack нет настройки MTU).
* Для `cps` память: клиенту dperf нужно `cps/ядро × 2 c × 80 B` hugepages на ядро; цель
  урезается автоматически (в логе будет предупреждение).
* Числа dperf `httpErr/skErr/retransmit` попадают в флаг `errors` — смотрите их в
  `results.jsonl`/логах точки, если флаг стоит.
* **FDIR на NIC клиента сильно предпочтителен.** В режиме `rss` (NIC без нужных rte_flow)
  ответы могут приходить не на то ядро dperf, которое открыло соединение: растут
  `tcpDrop/httpErr`, результаты шумнее и ниже реальных.
* **F-Stack + VXLAN: один outer UDP source port.** F-Stack не передаёт RSS-хеш NIC во FreeBSD
  (`m_pkthdr.flowid`), поэтому `vxlan(4)` берёт source port из хеша inner Ethernet-заголовка —
  он одинаковый для всех потоков. Следствие для `bond + VXLAN`: весь исходящий VXLAN-трафик
  F-Stack идёт через **один** слейв bond (хеш L3+L4 по outer-заголовку), а ядро Linux
  раскладывает потоки по разным source port и слейвам. Для `bw` это заметно; входящий трафик
  (от dperf) распределяется нормально.
* MAC интерфейса `vxlan0` в F-Stack нельзя задать через `ff_ifconfig` (ошибка ioctl); он
  детерминирован (hostuuid + имя интерфейса) и одинаков во всех процессах — стенд читает его
  и передаёт dperf как inner dst MAC.
* bond в F-Stack с несколькими воркерами: режимы 0–4 (в т.ч. 802.3ad); 5/6 (TLB/ALB) во
  вторичных процессах DPDK не поддерживаются.
* Не запускайте на сервере во время прогона другие DPDK-приложения с префиксом по
  умолчанию (`rte`): новый primary-процесс удаляет hugepage-файлы F-Stack, и `ff_top`
  (флаг `no-ff_top`) перестаёт подключаться.

## Диагностика
* `waiting for the agent` — не запущен `agent.sh` на B, не тот `server_addr`/порт, фаервол.
* `bench.conf differs` — разные конфиги на машинах (сверяется хеш секций network/run/scenario*).
* `dperf server did not start` / `bad gateway` — нет связи по тестовым портам (кабель, IP,
  bond/LACP); логи dperf лежат в `points/<точка>/`, на B — в `work/agent/`.
* `FDIR-UNSUPPORTED` — NIC не поддерживает нужные rte_flow; стенд сам перейдёт на `rss`.
* `stalled` — трафик встал (нет ответов за окно). Для F-Stack `bw` чаще всего это TSO
  конкретного NIC/драйвера: проверьте с `fstack_tso = 0` в `[scenario2]`.
* `no-ff_top` — не удалось снять CPU F-Stack через `ff_top`: утилиты собраны против другого
  DPDK (`./build.sh`), либо на машине работает другой DPDK primary с префиксом `rte`.
* `F-Stack nginx did not come up` — смотрите `points/<точка>/nginx-error.log.tail`; для bond
  `Invalid args in mode=…,slave=…` означает F-Stack без исправлений этой ветки.
* hugepages не выделились на лету — резервируйте при загрузке (см. cmdline выше).
* После аварии: `./restore.sh client` / `./restore.sh server`.

## Изменения в F-Stack
* `lib/ff_api.symlist`: добавлен экспорт `ff_mbuf_set_timestamp`. Без него текущая ветка
  F-Stack (после «Propagate DPDK mbuf RX timestamp…») не линкует ни одно приложение
  (`undefined reference to ff_mbuf_set_timestamp` при сборке nginx). `build.sh` также
  пересобирает старую `libfstack.a`, если видит эту ошибку.
* bond с встроенным DPDK 23.11 не работал вообще:
  * `lib/ff_config.c`: DPDK 23.11 переименовал devarg bonding `slave` → `member` и отвергает
    старый ключ (`Invalid args` при старте). F-Stack передаёт участников с ключом, который
    понимает используемый DPDK; в `[bondN]` можно писать и `slave=`, и `member=`.
  * `dpdk/drivers/net/bonding`: во вторичных процессах DPDK bonding PMD не настраивал
    функции rx/tx («TODO: request info from primary»), поэтому каждый воркер F-Stack, кроме
    первого, падал (SIGSEGV) на первом же `rx_burst`. Теперь вторичный процесс получает
    функции rx/tx режима, выбранного primary (режимы 0–4); состояние LACP по портам (mode 4)
    перенесено из локального массива процесса в memzone, общую для всех процессов; хеш
    балансировки выбирается по политике, а не через указатель на функцию в общей памяти.
  * `lib/ff_dpdk_if.c`: если NIC не может направить LACP-кадры в выделенную очередь, bond
    mode 4 отправляет LACPDU только изнутри `rte_eth_tx_burst()`, а F-Stack вызывал его
    только при наличии данных — LACP никогда не сходился. Теперь для портов bond mode 4
    раз в 10 мс вызывается пустой `tx_burst`.

## Изменения в dperf
`patches/dperf-vxlan-inner-tcp-csum.patch` (накладывается `build.sh` автоматически): в
VXLAN-режиме dperf пишет в заголовок `th_seq = snd_una`, а контрольную сумму inner TCP
пересчитывает от `snd_nxt`. Сумма неверна у SYN/FIN и сегментов с данными, Linux и F-Stack
их отбрасывают (`TcpInCsumErrors`), и сценарий 2 с VXLAN не работает совсем. В сценарии 1
(dperf ↔ dperf) ошибка не видна: dperf не проверяет сумму inner TCP.
