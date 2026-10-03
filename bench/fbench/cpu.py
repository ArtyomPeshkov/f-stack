"""CPU utilisation sampling from /proc/stat."""

import threading
import time

from .util import read_file


def snapshot():
    """{cpu_id: (user, nice, system, idle, iowait, irq, softirq, steal)}"""
    snap = {}
    for line in (read_file("/proc/stat", "") or "").splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        parts = line.split()
        try:
            cpu = int(parts[0][3:])
        except ValueError:
            continue
        vals = [int(x) for x in parts[1:9]] + [0] * (8 - len(parts[1:9]))
        snap[cpu] = vals
    return snap


def delta(a, b):
    """Per-CPU utilisation (fractions) between two snapshots."""
    out = {}
    for cpu, vb in b.items():
        va = a.get(cpu)
        if not va:
            continue
        d = [y - x for x, y in zip(va, vb)]
        total = float(sum(d)) or 1.0
        user, nice, system, idle, iowait, irq, softirq, steal = d
        out[cpu] = {
            "busy": (user + nice + system + irq + softirq + steal) / total,
            "usr": (user + nice) / total,
            "sys": system / total,
            "irq": (irq + softirq) / total,
        }
    return out


def aggregate(util, cores):
    sel = [util[c] for c in cores if c in util]
    other = [u for c, u in util.items() if c not in cores]
    return {
        "busy_cores_total": sum(u["busy"] for u in util.values()),
        "busy_cores_selected": sum(u["busy"] for u in sel),
        "busy_cores_other": sum(u["busy"] for u in other),
        "usr_cores": sum(u["usr"] for u in util.values()),
        "sys_cores": sum(u["sys"] for u in util.values()),
        "irq_cores": sum(u["irq"] for u in util.values()),
        "selected_max": max([u["busy"] for u in sel] or [0.0]),
        "per_core": {str(c): round(util[c]["busy"], 3) for c in cores if c in util},
    }


class Sampler(object):
    """Samples /proc/stat every second between start() and stop()."""

    def __init__(self, cores):
        self.cores = list(cores)
        self.series = []
        self._stop = threading.Event()
        self.t0 = time.time()
        self.first = snapshot()
        self._last = self.first
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.wait(1.0):
            cur = snapshot()
            agg = aggregate(delta(self._last, cur), self.cores)
            agg.pop("per_core", None)
            agg["t"] = time.time() - self.t0
            self.series.append(agg)
            self._last = cur

    def stop(self):
        self._stop.set()
        self._t.join(3)
        end = snapshot()
        res = aggregate(delta(self.first, end), self.cores)
        res["seconds"] = time.time() - self.t0
        res["series"] = self.series
        return res
