/*
 * Заглушка rte_pdump.h для DPDK, собранного без необязательной библиотеки pdump
 * (её может не быть в сборке под f-stack). dperf вызывает только init/uninit:
 * без них недоступен лишь захват пакетов утилитой dpdk-pdump, на замеры это не влияет.
 * Подключается build_dperf.sh, только если в DPDK нет настоящего rte_pdump.h.
 */
#ifndef PERF_TESTS_COMPAT_RTE_PDUMP_H
#define PERF_TESTS_COMPAT_RTE_PDUMP_H

static inline int rte_pdump_init(void)
{
    return 0;
}

static inline int rte_pdump_uninit(void)
{
    return 0;
}

#endif
