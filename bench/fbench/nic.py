"""NIC handling: driver binding (kernel <-> DPDK), interface discovery,
queues / IRQ affinity / XPS tuning for the kernel stack."""

import glob
import json
import os
import re
import time

from .util import debug, die, info, read_file, run, wait_until, warn, which, write_sys

DPDK_KMODS = ("vfio-pci", "igb_uio", "uio_pci_generic")
STATE_FILE = "/var/tmp/fbench-nic-drivers.json"


def pci_path(bdf):
    return "/sys/bus/pci/devices/%s" % bdf


def current_driver(bdf):
    link = os.path.join(pci_path(bdf), "driver")
    if not os.path.exists(link):
        return None
    return os.path.basename(os.path.realpath(link))


def numa_node(bdf):
    v = read_file(os.path.join(pci_path(bdf), "numa_node"), "-1").strip()
    try:
        return max(int(v), 0)
    except ValueError:
        return 0


def netdevs(bdf):
    d = os.path.join(pci_path(bdf), "net")
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def _save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except (IOError, OSError):
        pass


def remember_kernel_driver(bdf):
    drv = current_driver(bdf)
    if drv and drv not in DPDK_KMODS:
        st = _load_state()
        if st.get(bdf) != drv:
            st[bdf] = drv
            _save_state(st)
    return drv


def kernel_driver_for(bdf, configured="auto"):
    if configured and configured != "auto":
        return configured
    drv = current_driver(bdf)
    if drv and drv not in DPDK_KMODS:
        return drv
    st = _load_state()
    if bdf in st:
        return st[bdf]
    alias = read_file(os.path.join(pci_path(bdf), "modalias"), "").strip()
    if alias and which("modprobe"):
        rc, out = run(["modprobe", "-R", alias], check=False, quiet=True)
        mods = [m.strip() for m in out.splitlines() if m.strip() and m.strip() not in
                ("vfio_pci", "igb_uio", "uio_pci_generic")]
        if rc == 0 and mods:
            return mods[0]
    die("cannot determine kernel driver for %s; set kernel_driver in the config" % bdf)


def is_mellanox(bdf):
    vendor = read_file(os.path.join(pci_path(bdf), "vendor"), "").strip()
    return vendor == "0x15b3"


class NicManager(object):
    """Binding of the test ports of one machine."""

    def __init__(self, machine, fstack_dir, dpdk_build):
        self.m = machine
        self.devbind = os.path.join(fstack_dir, "dpdk", "usertools", "dpdk-devbind.py")
        self.dpdk_build = dpdk_build
        self.kdrivers = {}
        for i, bdf in enumerate(machine.pci):
            if not os.path.exists(pci_path(bdf)):
                die("PCI device %s (%s) not found" % (bdf, machine.role))
            remember_kernel_driver(bdf)

    # ---------------------------------------------------------------- binding
    def bifurcated(self):
        return self.m.dpdk_driver == "none"

    def _ensure_dpdk_kmod(self):
        drv = self.m.dpdk_driver
        if drv == "vfio-pci":
            run(["modprobe", "vfio-pci"], check=False)
            groups = glob.glob("/sys/kernel/iommu_groups/*")
            if not groups:
                warn("IOMMU is off: enabling vfio no-IOMMU mode (or boot with intel_iommu=on iommu=pt)")
                write_sys("/sys/module/vfio/parameters/enable_unsafe_noiommu_mode", "1")
        elif drv == "igb_uio":
            if not os.path.isdir("/sys/module/igb_uio"):
                run(["modprobe", "uio"], check=False)
                ko = None
                for cand in (os.path.join(self.dpdk_build, "kernel/linux/igb_uio/igb_uio.ko"),):
                    if os.path.isfile(cand):
                        ko = cand
                if ko:
                    run(["insmod", ko])
                else:
                    run(["modprobe", "igb_uio"])
        elif drv == "uio_pci_generic":
            run(["modprobe", "uio_pci_generic"])

    def _bind(self, bdf, drv):
        cur = current_driver(bdf)
        if cur == drv:
            return
        for dev in netdevs(bdf):
            run(["ip", "addr", "flush", "dev", dev], check=False, quiet=True)
            run(["ip", "link", "set", dev, "down"], check=False, quiet=True)
        info("binding %s: %s -> %s" % (bdf, cur, drv))
        if os.path.isfile(self.devbind):
            run(["python3", self.devbind, "--force", "-b", drv, bdf], timeout=60)
        else:
            # plain sysfs fallback
            if cur:
                write_sys(os.path.join(pci_path(bdf), "driver/unbind"), bdf)
            write_sys(os.path.join(pci_path(bdf), "driver_override"), drv)
            write_sys("/sys/bus/pci/drivers/%s/bind" % drv, bdf)
            write_sys(os.path.join(pci_path(bdf), "driver_override"), "\n")
        if current_driver(bdf) != drv:
            die("failed to bind %s to %s" % (bdf, drv))

    def bind_dpdk(self, ports=None):
        ports = self.m.pci if ports is None else ports
        if self.bifurcated():
            for bdf in ports:
                for dev in netdevs(bdf):
                    # bifurcated PMD (mlx5): keep the netdev up but without addresses
                    run(["ip", "addr", "flush", "dev", dev], check=False, quiet=True)
                    run(["ip", "link", "set", dev, "up"], check=False, quiet=True)
            return
        self._ensure_dpdk_kmod()
        for bdf in ports:
            remember_kernel_driver(bdf)
            self._bind(bdf, self.m.dpdk_driver)

    def bind_kernel(self, ports=None):
        ports = self.m.pci if ports is None else ports
        for bdf in ports:
            drv = kernel_driver_for(bdf, self.m.kernel_driver)
            if current_driver(bdf) != drv:
                run(["modprobe", drv], check=False, quiet=True)
                self._bind(bdf, drv)
            if not wait_until(lambda: netdevs(bdf), 20, what="netdev of %s" % bdf):
                die("no kernel interface appeared for %s" % bdf)
        return [self.ifname(i) for i in range(len(ports))]

    def ifname(self, idx):
        if idx < len(self.m.ifname) and self.m.ifname[idx]:
            return self.m.ifname[idx]
        devs = netdevs(self.m.pci[idx])
        if not devs:
            die("%s has no kernel interface (bound to %s); set ifname in [%s]" %
                (self.m.pci[idx], current_driver(self.m.pci[idx]), self.m.role))
        return devs[0]

    def status(self):
        out = []
        for bdf in self.m.pci:
            out.append({"pci": bdf, "driver": current_driver(bdf), "numa": numa_node(bdf),
                        "netdev": netdevs(bdf), "mellanox": is_mellanox(bdf)})
        return out


# ------------------------------------------------------------- kernel tuning
def link_info(ifname):
    res = {"ifname": ifname}
    if not which("ethtool"):
        return res
    rc, out = run(["ethtool", ifname], check=False, quiet=True)
    m = re.search(r"Speed:\s*(\S+)", out)
    if m:
        res["speed"] = m.group(1)
    rc, out = run(["ethtool", "-i", ifname], check=False, quiet=True)
    for key in ("driver", "version", "firmware-version", "bus-info"):
        m = re.search(r"^%s:\s*(.*)$" % key, out, re.M)
        if m:
            res[key] = m.group(1).strip()
    return res


_IRQ_SKIP = re.compile(r"(async|ctrl|fdir|misc|ptp|mbx|aux|cmd|pages|config|-ctl|_ctl)", re.I)


def _irq_queue_index(name):
    for pat in (r"comp(\d+)", r"(?:TxRx|rx|tx|queue|fp|input|output)[-_.]?(\d+)"):
        m = re.search(pat, name, re.I)
        if m:
            return int(m.group(1))
    return None


def device_irqs(ifname, bdf):
    """[(queue_index, irq)] of the data-path vectors of one port, sorted by queue."""
    msi = set()
    for p in glob.glob(os.path.join(pci_path(bdf), "msi_irqs", "*")):
        try:
            msi.add(int(os.path.basename(p)))
        except ValueError:
            pass
    found = []
    for line in (read_file("/proc/interrupts", "") or "").splitlines()[1:]:
        parts = line.split()
        if not parts or not parts[0].rstrip(":").isdigit():
            continue
        irq, name = int(parts[0].rstrip(":")), parts[-1]
        if irq not in msi and ifname not in name and bdf not in name:
            continue
        if name in (ifname, bdf) or _IRQ_SKIP.search(name):
            continue
        q = _irq_queue_index(name.split("@")[0])
        found.append((q if q is not None else 10000 + irq, irq))
    found.sort()
    return found


def set_channels(ifname, n):
    rc, out = run(["ethtool", "-l", ifname], check=False, quiet=True)
    m = re.search(r"Pre-set maximums:.*?Combined:\s*(\d+)", out, re.S)
    maxc = int(m.group(1)) if m else n
    n = max(1, min(n, maxc or n))
    rc, _ = run(["ethtool", "-L", ifname, "combined", str(n)], check=False, quiet=True)
    if rc != 0:
        run(["ethtool", "-L", ifname, "rx", str(n), "tx", str(n)], check=False, quiet=True)
    run(["ethtool", "-X", ifname, "equal", str(n)], check=False, quiet=True)
    return n


def tune_kernel_port(ifname, bdf, cores, vxlan):
    """channels = len(cores); IRQ q -> cores[q]; XPS tx-q -> cores[q]; RPS off."""
    n = set_channels(ifname, len(cores))
    time.sleep(1.0)
    irqs = device_irqs(ifname, bdf)
    if not irqs:
        warn("%s: no IRQs found, IRQ affinity left unchanged" % ifname)
    for i, (q, irq) in enumerate(irqs):
        core = cores[(q if q < 10000 else i) % len(cores)]
        write_sys("/proc/irq/%d/smp_affinity_list" % irq, str(core))
    qdir = "/sys/class/net/%s/queues" % ifname
    for p in glob.glob(os.path.join(qdir, "tx-*")):
        q = int(p.rsplit("-", 1)[1])
        core = cores[q % len(cores)]
        write_sys(os.path.join(p, "xps_cpus"), "%x" % (1 << core))
    for p in glob.glob(os.path.join(qdir, "rx-*")):
        write_sys(os.path.join(p, "rps_cpus"), "0")
    # RSS over UDP ports too (VXLAN outer header carries the flow entropy)
    run(["ethtool", "-N", ifname, "rx-flow-hash", "udp4", "sdfn"], check=False, quiet=True)
    run(["ethtool", "-K", ifname, "gro", "on", "lro", "off"], check=False, quiet=True)
    if vxlan:
        run(["ethtool", "-K", ifname, "rx-udp_tunnel-port-offload", "on"], check=False, quiet=True)
    debug("%s: channels=%d irqs=%s" % (ifname, n, irqs))
    return {"ifname": ifname, "channels": n, "irqs": len(irqs)}
