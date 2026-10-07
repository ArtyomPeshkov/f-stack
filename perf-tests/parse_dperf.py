#!/usr/bin/env python3
"""Парсер логов dperf.

  ./parse_dperf.py results/s1-...-client [results/s1-...-server] [--csv out.csv]
  ./parse_dperf.py results/s2-...-client [results/s2-...-server]
  ./parse_dperf.py --brief results/s1-...-client/bond0-vxlan0/n04.log
  ./parse_dperf.py dperf.out            # любой вывод dperf: stdout или /var/log/dperf/*.log

По каждому прогону клиента: средние за установившийся режим — секунды
[--skip, последняя - --tail], т.е. без разгона (slow_start) и останова.
Если передан каталог сервера, его загрузка CPU и ошибки берутся за то же окно
по меткам времени (часы машин должны быть синхронизированы, NTP): в сценарии 1 —
cpuUsage dperf, в сценарии 2 — загрузка ядер nginx (строки ngxcpu).
По каждому шагу (сценарий 1 — bond/vxlan/направление, сценарий 2 — стек и размер ответа) —
оптимум: минимальное число воркеров, дающее не меньше --opt от максимальной
пропускной способности шага. В сценарии 2 в итоге ещё и kernel против F-Stack.
"""
import argparse
import csv
import os
import re
import sys

ANSI = re.compile(r'\x1b\[[0-9;]*m')
STAMP = re.compile(r'^(\d{9,11}) ?(.*)$')
DROPS = ('dropTx', 'tcpDrop', 'udpDrop', 'badRx')         # счётчики в секунду
RETRANS = ('synRt', 'finRt', 'ackRt', 'pushRt', 'udpRt', 'skErr')
NIC_ERRORS = ('ierrors', 'oerrors', 'imissed')             # накопительные счётчики порта
UNITS = {'': 1, 'k': 1024, 'm': 1024 ** 2, 'g': 1024 ** 3}


def number(s):
    s = s.replace(',', '')
    try:
        return float(s) if '.' in s else int(s)
    except ValueError:
        return None


def size_bytes(s):
    """64k -> 65536; не размер -> 0."""
    m = re.match(r'^(\d+)([kmg]?)$', s or '', re.I)
    return int(m.group(1)) * UNITS[m.group(2).lower()] if m else 0


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
            if tok[0] == 'ngxcpu':           # s2_nginx.sh: загрузка ядер nginx за секунду, % по ядрам
                recs.append({'t': t, 'sec': len(recs), 'cpu': [float(x) for x in tok[1:]]})
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
        'size': meta.get('size', ''),            # сценарий 2: размер ответа nginx
        'mode': meta.get('mode', ''),            # сценарий 2: соединения и тело ответа, keepalive-file, ...
        'cores': cores,
        'gbps_rx': mean(r.get('bitsRx', 0) for r in w) / 1e9,
        'gbps_tx': mean(r.get('bitsTx', 0) for r in w) / 1e9,
        'mpps': mean(r.get('pktRx', 0) + r.get('pktTx', 0) for r in w) / 1e6,
        'krps': mean(r.get('http2XX', 0) for r in w) / 1e3,
        'kcps': mean(r.get('skOpen', 0) for r in w) / 1e3,
        'cpu_cli': mean(mean(r['cpu']) for r in w if r['cpu']),
        'cpu_srv': None,
        'drops': drops,
        'retr': retr,
        'http_err': sum(r.get('httpErr', 0) for r in w),
        'samples': len(w),
        't0': w[0]['t'],
        't1': w[-1]['t'],
    }
    s['gbps'] = s['gbps_rx'] + s['gbps_tx']
    # пропускная способность точки: в сценарии 2 — ответы nginx; в сценарии 1 — оба направления,
    # а в однонаправленных шагах — поток: в b2a машина A (её лог здесь) принимает, в a2b — отдаёт
    if s['size'] or meta.get('dir') == 'b2a':
        s['tput'] = s['gbps_rx']
    elif meta.get('dir') == 'a2b':
        s['tput'] = s['gbps_tx']
    else:
        s['tput'] = s['gbps']
    # подпись в таблицах сценария 2: размер, а для режима не по умолчанию — и режим, "600 close-return"
    s['label'] = s['size'] + ('' if s['mode'] in ('', 'keepalive-file') else ' ' + s['mode'])
    s['gbps_core'] = s['tput'] / cores
    return s


def attach_server(s, servers):
    """Загрузка CPU первых N воркеров сервера и его ошибки за окно замера клиента."""
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
    """-> [(файл, указан явно)]: каталоги обходятся рекурсивно по *.log."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [(os.path.join(root, n), False) for n in names if n.endswith('.log')]
        else:
            files.append((p, True))
    return sorted(files)


def pct(v):
    return '-' if v is None else '%.0f%%' % v


def group_key(s):
    return (s['session'], s['step'], s['label']) if s['size'] else (s['session'], s['step'])


def group_order(key):
    return key[:2] + ((size_bytes(key[2].split()[0]), key[2]) if len(key) > 2 else ())


def valid(r):
    """Сценарий 2: прогон, где nginx не ответил ни одним 200 (только ошибки), не в счёт."""
    return not r['size'] or r['krps'] > 0 or not r['http_err']


def pick_optimum(rows, opt):
    """-> (оптимум, максимум): минимум воркеров с >= opt от лучшего результата шага."""
    rows = [r for r in rows if valid(r)] or rows
    top = max(rows, key=lambda r: r['tput'])
    pick = next(r for r in rows if r['tput'] >= opt * top['tput'])
    return pick, top


def print_group(key, rows, pick, top):
    best = top['tput'] or 1e-9
    print('\n== %s' % ' / '.join(key))
    if len(key) > 2:                                       # сценарий 2: dperf -> nginx
        print('%6s %8s %9s %8s %10s %8s %8s %8s %7s %6s' % (
            'cores', 'Gbps', 'Krps', 'Kcps', 'Gbps/core', 'cpu_srv', 'cpu_cli', 'httpErr', 'drops', 'retr'))
        for r in rows:
            print('%6d %8.1f %9.1f %8.1f %10.1f %8s %8s %8d %7d %6d%s' % (
                r['cores'], r['gbps_rx'], r['krps'], r['kcps'], r['gbps_core'], pct(r['cpu_srv']), pct(r['cpu_cli']),
                r['http_err'], r['drops'], r['retr'], '  <- оптимум' if r is pick and valid(r) else ''))
        if valid(top):
            print('максимум %.1f Gbps (%.1f Krps) при N=%d; оптимум N=%d: %.1f Gbps (%.0f%% от максимума, %.1f Gbps на ядро)' % (
                top['tput'], top['krps'], top['cores'], pick['cores'], pick['tput'], 100 * pick['tput'] / best,
                pick['gbps_core']))
        if any(r['http_err'] for r in rows):
            print('httpErr > 0: nginx отвечал не 200 — на сервере другое число воркеров (клиент и сервер разошлись '
                  'по точкам) или нет файлов ответов')
        return
    print('%6s %8s %8s %9s %7s %10s %8s %8s %7s %6s' % (
        'cores', 'Gbps_rx', 'Gbps_tx', 'Gbps_sum', 'Mpps', 'Gbps/core', 'cpu_cli', 'cpu_srv', 'drops', 'retr'))
    for r in rows:
        print('%6d %8.1f %8.1f %9.1f %7.1f %10.1f %8s %8s %7d %6d%s' % (
            r['cores'], r['gbps_rx'], r['gbps_tx'], r['gbps'], r['mpps'], r['gbps_core'],
            pct(r['cpu_cli']), pct(r['cpu_srv']), r['drops'], r['retr'], '  <- оптимум' if r is pick else ''))
    print('максимум %.1f Gbps при N=%d; оптимум N=%d: %.1f Gbps (%.0f%% от максимума, %.1f Gbps на ядро)' % (
        top['tput'], top['cores'], pick['cores'], pick['tput'], 100 * pick['tput'] / best, pick['gbps_core']))


def print_compare(rows):
    """Сценарий 2: kernel против F-Stack при одном и том же размере ответа и числе воркеров nginx.
    Сессия с обоими стеками сравнивается сама с собой. Сессии с одним стеком (S2_STACKS=kernel
    и S2_STACKS=fstack запускали отдельно) — между собой; при повторах точки берётся более поздняя."""
    rows = sorted((r for r in rows if r['size']), key=lambda r: r['session'])
    stacks = {}
    for r in rows:
        stacks.setdefault(r['session'], set()).add(r['step'])
    single = [x for x in stacks if not {'kernel', 'fstack'} <= stacks[x]]
    for session in stacks:
        if session not in single:
            compare_table(session, [r for r in rows if r['session'] == session])
    compare_table(' + '.join(single), [r for r in rows if r['session'] in single])


def compare_table(title, rows):
    by = {(r['label'], r['cores'], r['step']): r for r in rows}
    if not {'kernel', 'fstack'} <= {k[2] for k in by}:
        return
    width = max(6, max(len(k[0]) for k in by))
    fmt = '%*s %5s %9s %9s %9s %8s %8s %8s %8s %8s %8s'
    print('\n== kernel против F-Stack: %s' % title)
    print(fmt % (width, 'size', 'cores', 'kern_Gbps', 'kern_Krps', 'kern_Kcps', 'kern_cpu',
                 'ff_Gbps', 'ff_Krps', 'ff_Kcps', 'ff_cpu', 'ff/kern'))
    for label, cores in sorted({k[:2] for k in by}, key=lambda k: (size_bytes(k[0].split()[0]), k[0], k[1])):
        k, f = by.get((label, cores, 'kernel')), by.get((label, cores, 'fstack'))
        cols = []
        for r in (k, f):
            cols += (['%.1f' % r['gbps_rx'], '%.1f' % r['krps'], '%.1f' % r['kcps'], pct(r['cpu_srv'])]
                     if r else ['-'] * 4)
        ratio = '%.2fx' % (f['krps'] / k['krps']) if k and f and k['krps'] and f['krps'] else '-'
        print(fmt % tuple([width, label, cores] + cols + [ratio]))


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
    for f, explicit in collect(args.paths):
        meta, recs = parse_log(f)
        if not recs:
            # в каталогах бывают и чужие *.log (например, error.log nginx) — о них молчим
            if meta.get('scenario') == 's2' and meta.get('role') == 'server':
                print('%s: нет замеров CPU (клиент не дал трафика или не сработал ff_top)' % f)
            elif meta or explicit:
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
            if s['size']:
                print('%s: %.1f Gbps  %.1f Krps  %.1f Kcps  cpu_cli %s  httpErr %d  drops %d  retr %d' % (
                    os.path.basename(s['file']), s['gbps_rx'], s['krps'], s['kcps'], pct(s['cpu_cli']),
                    s['http_err'], s['drops'], s['retr']))
            else:
                print('%s: rx %.1f  tx %.1f  sum %.1f Gbps  %.1f Mpps  cpu %s  drops %d  retr %d' % (
                    os.path.basename(s['file']), s['gbps_rx'], s['gbps_tx'], s['gbps'], s['mpps'],
                    pct(s['cpu_cli']), s['drops'], s['retr']))
        return

    groups = {}
    for s in rows:
        groups.setdefault(group_key(s), []).append(s)
    summary = []
    for key in sorted(groups, key=group_order):
        g = sorted(groups[key], key=lambda r: r['cores'])
        summary.append((key, g) + pick_optimum(g, args.opt))
        if not args.summary:
            print_group(key, g, *summary[-1][2:])

    if len(summary) > 1 or args.summary:
        print('\n== Итог: оптимум — минимум воркеров с >= %.0f%% от максимума шага' % (100 * args.opt))
        width = max(len(' / '.join(key)) for key, *_ in summary)
        print('%-*s %9s %10s %9s %8s %8s' % (width, 'сессия / шаг', 'max_Gbps', 'opt_cores', 'opt_Gbps', 'cpu_cli', 'cpu_srv'))
        for key, g, pick, top in summary:
            if not valid(top):
                print('%-*s  нет ответов 200 (httpErr)' % (width, ' / '.join(key)))
                continue
            print('%-*s %9.1f %10d %9.1f %8s %8s' % (
                width, ' / '.join(key), top['tput'], pick['cores'], pick['tput'], pct(pick['cpu_cli']), pct(pick['cpu_srv'])))
        print_compare(rows)

    if args.csv:
        cols = ['session', 'step', 'size', 'mode', 'cores', 'gbps_rx', 'gbps_tx', 'gbps', 'mpps', 'krps', 'kcps',
                'gbps_core', 'cpu_cli', 'cpu_srv', 'drops', 'retr', 'http_err', 'samples', 'file']
        with open(args.csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for s in sorted(rows, key=lambda r: (r['session'], r['step'], size_bytes(r['size']), r['label'], r['cores'])):
                w.writerow({k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()})
        print('\nCSV: %s' % args.csv)


if __name__ == '__main__':
    sys.exit(main())
