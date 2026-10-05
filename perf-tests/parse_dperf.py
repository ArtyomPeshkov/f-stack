#!/usr/bin/env python3
"""Парсер логов dperf.

  ./parse_dperf.py results/s1-...-client [results/s1-...-server] [--csv out.csv]
  ./parse_dperf.py --brief results/s1-...-client/bond0-vxlan0/n04.log
  ./parse_dperf.py dperf.out            # любой вывод dperf: stdout или /var/log/dperf/*.log

По каждому прогону клиента: средние за установившийся режим — секунды
[--skip, последняя - --tail], т.е. без разгона (slow_start) и останова.
Если передан каталог сервера, его cpuUsage и ошибки берутся за то же окно
по меткам времени (часы машин должны быть синхронизированы, NTP).
По каждому шагу (bond/vxlan) — оптимум: минимальное число воркеров, дающее
не меньше --opt от максимальной пропускной способности шага.
"""
import argparse
import csv
import os
import re
import sys

ANSI = re.compile(r'\x1b\[[0-9;]*m')
STAMP = re.compile(r'^(\d{9,11}) ?(.*)$')
DROPS = ('dropTx', 'tcpDrop', 'udpDrop', 'badRx')         # счётчики в секунду
RETRANS = ('synRt', 'finRt', 'ackRt', 'pushRt', 'udpRt', 'skErr', 'httpErr')
NIC_ERRORS = ('ierrors', 'oerrors', 'imissed')             # накопительные счётчики порта


def number(s):
    s = s.replace(',', '')
    try:
        return float(s) if '.' in s else int(s)
    except ValueError:
        return None


def parse_log(path):
    """-> (meta, [секунды]); секунда — dict: t, sec, cpu[], pktRx, bitsRx, ..."""
    meta, recs, cur = {}, [], None
    with open(path, errors='replace') as f:
        for line in f:
            line = ANSI.sub('', line.rstrip('\n'))
            if line.startswith('#'):
                meta.update(kv.split('=', 1) for kv in line[1:].split() if '=' in kv)
                continue
            t = None
            m = STAMP.match(line)
            if m:
                t, line = int(m.group(1)), m.group(2)
            tok = line.split()
            if not tok:
                continue
            if tok[0] == 'Total':            # "Total Numbers:" — итоги за весь прогон, пропускаем
                cur = None
                continue
            if tok[0] == 'seconds':
                cur = {'t': t, 'sec': int(tok[1]), 'cpu': [int(x) for x in tok[3:] if x.isdigit()]}
                if recs and cur['sec'] < recs[-1]['sec']:   # daemon-лог дописывается: берём последний прогон
                    recs = []
                recs.append(cur)
            elif cur is not None:
                for k, v in zip(tok[0::2], tok[1::2]):
                    val = number(v)
                    if val is not None:
                        cur[k] = val
    return meta, recs


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def errors(w):
    drops = sum(r.get(k, 0) for r in w for k in DROPS)
    drops += sum(w[-1].get(k, 0) - w[0].get(k, 0) for k in NIC_ERRORS)
    retr = sum(r.get(k, 0) for r in w for k in RETRANS)
    return drops, retr


def summarize(path, meta, recs, args):
    last = recs[-1]['sec']
    w = [r for r in recs if args.skip <= r['sec'] <= last - args.tail]
    if not w:                                    # короткий прогон — берём середину
        w = recs[len(recs) // 4:max(len(recs) * 3 // 4, len(recs) // 4 + 1)]
    cores = int(meta.get('cores') or len(recs[0]['cpu']) or 1)
    drops, retr = errors(w)
    s = {
        'file': path,
        'session': os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path)))),
        'step': meta.get('step') or os.path.basename(os.path.dirname(os.path.abspath(path))),
        'cores': cores,
        'gbps_rx': mean(r.get('bitsRx', 0) for r in w) / 1e9,
        'gbps_tx': mean(r.get('bitsTx', 0) for r in w) / 1e9,
        'mpps': mean(r.get('pktRx', 0) + r.get('pktTx', 0) for r in w) / 1e6,
        'mrps': mean(r.get('http2XX', r.get('tcpRsp', 0)) for r in w) / 1e6,
        'cpu_cli': mean(mean(r['cpu']) for r in w if r['cpu']),
        'cpu_srv': None,
        'drops': drops,
        'retr': retr,
        'samples': len(w),
        't0': w[0]['t'],
        't1': w[-1]['t'],
    }
    s['gbps'] = s['gbps_rx'] + s['gbps_tx']
    s['gbps_core'] = s['gbps'] / cores
    return s


def attach_server(s, servers):
    """cpuUsage первых N воркеров сервера и его ошибки за окно замера клиента."""
    if s['t0'] is None:
        return
    for meta, recs in servers:
        if meta.get('step', s['step']) != s['step']:
            continue
        timed = [r for r in recs if r['t'] is not None]
        w = ([r for r in timed if s['t0'] + 1 <= r['t'] <= s['t1'] - 1] or
             [r for r in timed if s['t0'] <= r['t'] <= s['t1']])
        if not w:
            continue
        s['cpu_srv'] = mean(mean(r['cpu'][:s['cores']]) for r in w if r['cpu'])
        drops, retr = errors(w)
        s['drops'] += drops
        s['retr'] += retr
        return


def collect(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in names if n.endswith('.log')]
        else:
            files.append(p)
    return sorted(files)


def pct(v):
    return '-' if v is None else '%.0f%%' % v


def pick_optimum(rows, opt):
    """-> (оптимум, максимум): минимум воркеров с >= opt от лучшего результата шага."""
    top = max(rows, key=lambda r: r['gbps'])
    pick = next(r for r in rows if r['gbps'] >= opt * top['gbps'])
    return pick, top


def print_group(key, rows, pick, top):
    best = top['gbps'] or 1e-9
    print('\n== %s / %s' % key)
    print('%6s %8s %8s %9s %7s %10s %8s %8s %7s %6s' % (
        'cores', 'Gbps_rx', 'Gbps_tx', 'Gbps_sum', 'Mpps', 'Gbps/core', 'cpu_cli', 'cpu_srv', 'drops', 'retr'))
    for r in rows:
        print('%6d %8.1f %8.1f %9.1f %7.1f %10.1f %8s %8s %7d %6d%s' % (
            r['cores'], r['gbps_rx'], r['gbps_tx'], r['gbps'], r['mpps'], r['gbps_core'],
            pct(r['cpu_cli']), pct(r['cpu_srv']), r['drops'], r['retr'], '  <- оптимум' if r is pick else ''))
    print('максимум %.1f Gbps при N=%d; оптимум N=%d: %.1f Gbps (%.0f%% от максимума, %.1f Gbps на ядро)' % (
        top['gbps'], top['cores'], pick['cores'], pick['gbps'], 100 * pick['gbps'] / best, pick['gbps_core']))


def main():
    ap = argparse.ArgumentParser(description='Сводка по логам dperf')
    ap.add_argument('paths', nargs='+', help='лог-файлы или каталоги (рекурсивно *.log)')
    ap.add_argument('--skip', type=int, default=15, help='пропустить первые N секунд (разгон), по умолчанию 15')
    ap.add_argument('--tail', type=int, default=5, help='отбросить последние N секунд (останов), по умолчанию 5')
    ap.add_argument('--opt', type=float, default=0.95, help='порог оптимума от максимума шага, по умолчанию 0.95')
    ap.add_argument('--csv', help='записать все прогоны в CSV')
    ap.add_argument('--brief', action='store_true', help='одна строка на лог')
    ap.add_argument('--summary', action='store_true', help='только сводная таблица по шагам')
    args = ap.parse_args()

    clients, servers = [], []
    for f in collect(args.paths):
        meta, recs = parse_log(f)
        if not recs:
            print('%s: нет секундной статистики (dperf не стартовал?)' % f)
            continue
        (servers if meta.get('role') == 'server' else clients).append((f, meta, recs))
    if not clients:
        if servers:
            print('только логи сервера: добавьте каталог клиента, сервер сводится по окнам его прогонов')
        return 1

    rows = [summarize(f, meta, recs, args) for f, meta, recs in clients]
    for s in rows:
        attach_server(s, [(m, r) for _, m, r in servers])

    if args.brief:
        for s in rows:
            print('%s: rx %.1f  tx %.1f  sum %.1f Gbps  %.1f Mpps  cpu %s  drops %d  retr %d' % (
                os.path.basename(s['file']), s['gbps_rx'], s['gbps_tx'], s['gbps'], s['mpps'],
                pct(s['cpu_cli']), s['drops'], s['retr']))
        return

    groups = {}
    for s in rows:
        groups.setdefault((s['session'], s['step']), []).append(s)
    summary = []
    for key in sorted(groups):
        g = sorted(groups[key], key=lambda r: r['cores'])
        summary.append((key, g) + pick_optimum(g, args.opt))
        if not args.summary:
            print_group(key, g, *summary[-1][2:])

    if len(summary) > 1 or args.summary:
        print('\n== Итог: оптимум — минимум воркеров с >= %.0f%% от максимума шага' % (100 * args.opt))
        width = max(len('%s / %s' % key) for key, *_ in summary)
        print('%-*s %9s %10s %9s %8s %8s' % (width, 'сессия / шаг', 'max_Gbps', 'opt_cores', 'opt_Gbps', 'cpu_cli', 'cpu_srv'))
        for key, g, pick, top in summary:
            print('%-*s %9.1f %10d %9.1f %8s %8s' % (
                width, '%s / %s' % key, top['gbps'], pick['cores'], pick['gbps'], pct(pick['cpu_cli']), pct(pick['cpu_srv'])))

    if args.csv:
        cols = ['session', 'step', 'cores', 'gbps_rx', 'gbps_tx', 'gbps', 'mpps', 'mrps', 'gbps_core',
                'cpu_cli', 'cpu_srv', 'drops', 'retr', 'samples', 'file']
        with open(args.csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for s in sorted(rows, key=lambda r: (r['session'], r['step'], r['cores'])):
                w.writerow({k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()})
        print('\nCSV: %s' % args.csv)


if __name__ == '__main__':
    sys.exit(main())
