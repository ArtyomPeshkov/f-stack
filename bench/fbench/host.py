"""Host preparation (hugepages, CPU power management, irqbalance, sysctl)
and host information / sanity checks."""

import glob
import os
import platform
import re
import socket
import struct

from .util import debug, info, read_file, run, warn, which, write_sys
from . import nic as nicmod


# --------------------------------------------------------------- hugepages
def _hp_dir(node, size_kb):
    d = "/sys/devices/system/node/node%d/hugepages/hugepages-%dkB" % (node, size_kb)
    if os.path.isdir(d):
        return d
    d = "/sys/kernel/mm/hugepages/hugepages-%dkB" % size_kb
    return d if os.path.isdir(d) else None


def _hp_count(d):
    try:
        return int(read_file(os.path.join(d, "nr_hugepages"), "0").strip())
    except ValueError:
        return 0


def _hugetlbfs_mounts():
    mounts = {}
    for line in (read_file("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "hugetlbfs":
            m = re.search(r"pagesize=(\d+)([KMG])", parts[3])
            if m:
                size_kb = int(m.group(1)) * {"K": 1, "M": 1024, "G": 1024 * 1024}[m.group(2)]
            else:
                size_kb = _default_hp_kb()
            mounts.setdefault(size_kb, parts[1])
    return mounts


def _default_hp_kb():
    m = re.search(r"Hugepagesize:\s*(\d+)", read_file("/proc/meminfo", "") or "")
    return int(m.group(1)) if m else 2048


def ensure_hugepages(gb, node):
    """Reserve `gb` GiB of hugepages on NUMA `node`: 1G pages first (fewer TLB
    misses), the remainder with 2M pages."""
    want_mb = gb * 1024
    got_mb = 0
    used = []
    d1 = _hp_dir(node, 1048576)
    if d1:
        cur = _hp_count(d1)
        if cur < gb:
            write_sys(os.path.join(d1, "nr_hugepages"), str(gb))
            cur = _hp_count(d1)
        if cur:
            used.append((1048576, cur))
            got_mb += cur * 1024
    if got_mb < want_mb:
        d2 = _hp_dir(node, 2048)
        if d2:
            need = (want_mb - got_mb) // 2
            cur = _hp_count(d2)
            if cur < need:
                write_sys(os.path.join(d2, "nr_hugepages"), str(need))
                cur = _hp_count(d2)
            if cur:
                used.append((2048, cur))
                got_mb += cur * 2
    mounts = _hugetlbfs_mounts()
    for size_kb, _ in used:
        if size_kb not in mounts:
            mnt = "/mnt/fbench-huge-%s" % ("1G" if size_kb == 1048576 else "2M")
            os.makedirs(mnt, exist_ok=True)
            run(["mount", "-t", "hugetlbfs", "-o", "pagesize=%dK" % size_kb, "nodev", mnt],
                check=False)
    desc = ", ".join("%d x %s" % (n, "1G" if s == 1048576 else "2M") for s, n in used) or "none"
    if got_mb < want_mb:
        warn("hugepages on node %d: %s (%d MiB < wanted %d MiB). Reserve them at boot: "
             "default_hugepagesz=1G hugepagesz=1G hugepages=%d" % (node, desc, got_mb, want_mb, gb))
    else:
        info("hugepages on node %d: %s" % (node, desc))
    return {"ok": got_mb >= want_mb, "mb": got_mb, "node": node, "pages": desc}


def free_hugepages_mb(node):
    total = 0
    for size_kb in (1048576, 2048):
        d = _hp_dir(node, size_kb)
        if d:
            try:
                free = int(read_file(os.path.join(d, "free_hugepages"), "0").strip())
            except ValueError:
                free = 0
            total += free * size_kb // 1024
    return total


# ------------------------------------------------------- CPU power settings
class PowerSettings(object):
    """performance governor + PM QoS (no deep C-states) while the run lasts."""

    def __init__(self):
        self.saved_gov = {}
        self.qos = None

    def apply(self):
        for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"):
            old = (read_file(p, "") or "").strip()
            if old and old != "performance":
                if write_sys(p, "performance"):
                    self.saved_gov[p] = old
        if self.saved_gov:
            info("cpufreq governor -> performance on %d CPUs" % len(self.saved_gov))
        try:
            self.qos = open("/dev/cpu_dma_latency", "wb", buffering=0)
            self.qos.write(struct.pack("i", 0))
            debug("PM QoS cpu_dma_latency=0 held")
        except (IOError, OSError) as e:
            debug("cannot hold /dev/cpu_dma_latency: %s" % e)
            self.qos = None

    def restore(self):
        for p, old in self.saved_gov.items():
            write_sys(p, old)
        self.saved_gov = {}
        if self.qos:
            try:
                self.qos.close()
            except Exception:
                pass
            self.qos = None


class IrqBalance(object):
    def __init__(self):
        self.stopped = False

    def stop(self):
        if which("systemctl"):
            rc, out = run(["systemctl", "is-active", "irqbalance"], check=False, quiet=True)
            if out.strip() == "active":
                run(["systemctl", "stop", "irqbalance"], check=False)
                self.stopped = True
                info("irqbalance stopped (will be restarted at the end)")
                return
        rc, _ = run(["pkill", "-x", "irqbalance"], check=False, quiet=True)
        if rc == 0:
            warn("irqbalance killed (no systemd); restart it manually if needed")

    def restore(self):
        if self.stopped:
            run(["systemctl", "start", "irqbalance"], check=False)
            self.stopped = False


class Sysctl(object):
    def __init__(self):
        self.saved = {}

    @staticmethod
    def _path(key):
        return "/proc/sys/" + key.replace(".", "/")

    def set(self, key, value):
        p = self._path(key)
        if not os.path.exists(p):
            debug("sysctl %s not present" % key)
            return
        old = (read_file(p, "") or "").strip()
        if key not in self.saved:
            self.saved[key] = old
        write_sys(p, str(value))

    def restore(self):
        for key, old in self.saved.items():
            write_sys(self._path(key), old)
        self.saved = {}


KERNEL_SYSCTLS = [
    ("net.core.somaxconn", 65535),
    ("net.core.netdev_max_backlog", 250000),
    ("net.core.rmem_max", 16777216),
    ("net.core.wmem_max", 16777216),
    ("net.ipv4.tcp_rmem", "4096 87380 16777216"),
    ("net.ipv4.tcp_wmem", "4096 65536 16777216"),
    ("net.ipv4.tcp_max_syn_backlog", 262144),
    ("net.ipv4.tcp_fin_timeout", 10),
    ("net.ipv4.tcp_max_tw_buckets", 2000000),
    ("net.ipv4.tcp_tw_reuse", 1),
    ("net.ipv4.tcp_slow_start_after_idle", 0),
    ("net.ipv4.tcp_timestamps", 1),
    ("net.ipv4.conf.all.rp_filter", 0),
    ("net.ipv4.conf.default.rp_filter", 0),
    ("net.ipv4.ip_forward", 0),
    ("fs.file-max", 4194304),
    ("fs.nr_open", 4194304),
    ("net.netfilter.nf_conntrack_max", 4194304),
]


# ------------------------------------------------------------ host info
def cpu_model():
    m = re.search(r"model name\s*:\s*(.*)", read_file("/proc/cpuinfo", "") or "")
    return m.group(1).strip() if m else platform.processor()


def cpu_numa(cpu):
    for d in glob.glob("/sys/devices/system/cpu/cpu%d/node*" % cpu):
        try:
            return int(os.path.basename(d)[4:])
        except ValueError:
            pass
    return 0


def thread_siblings(cpu):
    from .util import parse_cpu_list
    txt = read_file("/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list" % cpu, "")
    return parse_cpu_list((txt or "").strip()) or [cpu]


def online_cpus():
    from .util import parse_cpu_list
    return parse_cpu_list((read_file("/sys/devices/system/cpu/online", "0") or "0").strip())


def cmdline_param(name):
    m = re.search(r"(?:^|\s)%s=(\S+)" % re.escape(name), read_file("/proc/cmdline", "") or "")
    return m.group(1) if m else None


def host_info(machine):
    from .util import parse_cpu_list
    hi = {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "cpu_model": cpu_model(),
        "online_cpus": len(online_cpus()),
        "cmdline": (read_file("/proc/cmdline", "") or "").strip(),
        "cores": machine.cores,
        "nics": [],
    }
    for bdf in machine.pci:
        d = {"pci": bdf, "driver": nicmod.current_driver(bdf), "numa": nicmod.numa_node(bdf)}
        devs = nicmod.netdevs(bdf)
        if devs:
            d.update(nicmod.link_info(devs[0]))
        hi["nics"].append(d)
    hi["turbo"] = (read_file("/sys/devices/system/cpu/intel_pstate/no_turbo", "") or "").strip()
    gov = read_file("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor" % machine.cores[0], "")
    hi["governor"] = (gov or "").strip()
    hi["isolcpus"] = cmdline_param("isolcpus")
    hi["nohz_full"] = cmdline_param("nohz_full")
    hi["rcu_nocbs"] = cmdline_param("rcu_nocbs")
    hi["warnings"] = check_host(machine, hi)
    return hi


def check_host(machine, hi=None):
    from .util import parse_cpu_list
    warns = []
    online = set(online_cpus())
    for c in machine.cores:
        if c not in online:
            warns.append("core %d is not online" % c)
    if 0 in machine.cores:
        warns.append("core 0 is in the DPDK core list (housekeeping/IRQ noise); better exclude it")
    nic_node = nicmod.numa_node(machine.pci[0]) if machine.pci else 0
    remote = [c for c in machine.cores if cpu_numa(c) != nic_node]
    if remote:
        warns.append("cores %s are not on the NIC NUMA node %d (cross-NUMA traffic)" % (remote, nic_node))
    sib = []
    for c in machine.cores:
        for s in thread_siblings(c):
            if s != c and s in machine.cores and (s, c) not in sib:
                sib.append((c, s))
    if sib:
        warns.append("hyper-thread siblings both used: %s (use one thread per physical core "
                     "for per-core numbers)" % sib)
    for name in ("isolcpus", "nohz_full", "rcu_nocbs"):
        v = cmdline_param(name)
        lst = set(parse_cpu_list(re.sub(r"^[a-z_,]+,", "", v))) if v else set()
        if not set(machine.cores) <= lst:
            warns.append("%s does not cover the benchmark cores (recommended: %s=%s)" %
                         (name, name, ",".join(str(c) for c in machine.cores)))
    if machine.role == "server" and os.path.isdir("/sys/module/nf_conntrack"):
        warns.append("nf_conntrack is loaded: kernel-stack CPS numbers will be lower "
                     "(conntrack per connection); unload it or add NOTRACK rules for a clean test")
    return warns
