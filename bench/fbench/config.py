"""bench.conf loading. The same file is used on both machines:
[client] describes the load generator machine, [server] the machine under test."""

import configparser
import hashlib
import ipaddress
import os

from .util import die, parse_cpu_list, parse_rate, parse_size, split_words

# option -> default; every option of every section must be listed here
SCHEMA = {
    "paths": {
        "fstack_dir": "..",
        "dperf_dir": "../../dperf",
        "dpdk_build": "../build/dpdk-build",
        "dperf_bin": "",
        "nginx_fstack": "",
        "nginx_kernel": "",
        "fstack_tools": "",
        "work_dir": "./work",
        "results_dir": "./results",
    },
    "control": {
        "server_addr": "",
        "port": "9777",
        "token": "change-me",
        "ssh": "",
        "remote_dir": "",
    },
    "client": {
        "pci": "",
        "ifname": "",
        "cores": "",
        "hugepages_gb": "8",
        "dpdk_driver": "vfio-pci",
        "kernel_driver": "auto",
    },
    "server": {
        "pci": "",
        "ifname": "",
        "cores": "",
        "hugepages_gb": "8",
        "dpdk_driver": "vfio-pci",
        "kernel_driver": "auto",
    },
    "network": {
        "server_ip": "10.200.0.1",
        "client_ip": "10.200.0.2",
        "server_ip2": "10.201.0.1",
        "client_ip2": "10.201.0.2",
        "prefix": "16",
        "client_range": "10.210.0.1",
        "client_range2": "10.211.128.1",
        "server_range": "10.220.0.1",
        "server_range2": "10.221.0.1",
        "vni": "100",
        "inner_server_ip": "10.230.0.1",
        "inner_server_ip2": "10.231.0.1",
        "server_inner_mac": "02:fb:00:00:00:01",
        "client_inner_mac": "02:fb:00:00:00:02",
        "bond_mode": "4",
        "bond_xmit_policy": "l34",
    },
    "run": {
        "topologies": "single bond",
        "vxlan": "off on",
        "duration": "30",
        "ramp": "10",
        "settle": "5",
        "cooldown": "3",
        "optimum_tolerance": "5",
        "restore_bindings": "yes",
    },
    "scenario1": {
        "tests": "tps pps cps",
        "cores": "1 2 4 6 8 12",
        "jumbo": "off",
        "tps_payload": "1400",
        "tps_cc_per_core": "4000",
        "pps_payload": "18",
        "pps_cc_per_core": "20000",
        "cps_per_core": "2m",
        "cps_ramp": "30",
        "bw_payload": "64k",
        "bw_cc_per_core": "256",
    },
    "scenario2": {
        "stacks": "kernel fstack",
        "tests": "rps cps bw",
        "workers": "1 2 4 6 8 12",
        "client_cores": "12",
        "small_body": "600",
        "bw_size": "1m",
        "rps_cc_per_worker": "2000",
        "bw_cc_per_worker": "128",
        "cps_per_worker": "300k",
        "cps_ramp": "30",
        "fstack_tso": "1",
        "fstack_pkt_tx_delay": "0",
        "fstack_sendspace": "262144",
        "fstack_recvspace": "262144",
        "kernel_sendfile": "on",
        "nginx_output_buffers": "2 128k",
    },
}

TOPOLOGIES = ("single", "bond", "dual")
S1_TESTS = ("tps", "pps", "cps", "bw")
S2_TESTS = ("rps", "cps", "bw")
STACKS = ("kernel", "fstack")
DPDK_DRIVERS = ("vfio-pci", "igb_uio", "uio_pci_generic", "none")


def _yes(v):
    return str(v).strip().lower() in ("1", "yes", "on", "true")


class Machine(object):
    def __init__(self, role, sec):
        self.role = role
        self.pci = split_words(sec["pci"])
        self.ifname = split_words(sec["ifname"])
        self.cores = parse_cpu_list(sec["cores"])
        self.hugepages_gb = int(sec["hugepages_gb"])
        self.dpdk_driver = sec["dpdk_driver"].strip()
        self.kernel_driver = sec["kernel_driver"].strip()

    def validate(self):
        r = self.role
        if not self.pci:
            die("[%s] pci is empty" % r)
        for p in self.pci:
            if len(p) != 12 or p.count(":") != 2 or "." not in p:
                die("[%s] bad PCI address %r, expected full form like 0000:3b:00.0" % (r, p))
        if not self.cores:
            die("[%s] cores is empty" % r)
        if self.dpdk_driver not in DPDK_DRIVERS:
            die("[%s] dpdk_driver must be one of %s" % (r, ", ".join(DPDK_DRIVERS)))

    def as_dict(self):
        return {"pci": self.pci, "ifname": self.ifname, "cores": self.cores,
                "hugepages_gb": self.hugepages_gb, "dpdk_driver": self.dpdk_driver,
                "kernel_driver": self.kernel_driver}


class Config(object):
    def __init__(self, path):
        if not os.path.isfile(path):
            die("config file not found: %s (copy bench.conf.example to bench.conf)" % path)
        self.path = os.path.abspath(path)
        self.base = os.path.dirname(self.path)
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None,
                                       delimiters=("=",))
        cp.optionxform = str
        with open(self.path, encoding="utf-8") as f:
            self.text = f.read()
        cp.read_string(self.text)
        self.raw = {}
        for sec, opts in SCHEMA.items():
            vals = dict(opts)
            if cp.has_section(sec):
                for k, v in cp.items(sec):
                    if k not in opts:
                        die("unknown option [%s] %s in %s" % (sec, k, path))
                    vals[k] = v.strip()
            self.raw[sec] = vals
        for sec in cp.sections():
            if sec not in SCHEMA:
                die("unknown section [%s] in %s" % (sec, path))

        p = self.raw["paths"]
        self.fstack_dir = self._abs(p["fstack_dir"])
        self.dperf_dir = self._abs(p["dperf_dir"])
        self.dpdk_build = self._abs(p["dpdk_build"])
        self.work_dir = self._abs(p["work_dir"])
        self.results_dir = self._abs(p["results_dir"])
        self.install_dir = os.path.join(self.work_dir, "install")
        self.dperf_bin = self._abs(p["dperf_bin"]) if p["dperf_bin"] else \
            os.path.join(self.dperf_dir, "build", "dperf")
        self.nginx_fstack = self._abs(p["nginx_fstack"]) if p["nginx_fstack"] else \
            os.path.join(self.install_dir, "nginx-fstack", "sbin", "nginx")
        self.nginx_kernel = self._abs(p["nginx_kernel"]) if p["nginx_kernel"] else \
            os.path.join(self.install_dir, "nginx-kernel", "sbin", "nginx")
        self.fstack_tools = self._abs(p["fstack_tools"]) if p["fstack_tools"] else \
            os.path.join(self.fstack_dir, "tools", "sbin")

        c = self.raw["control"]
        self.server_addr = c["server_addr"]
        self.port = int(c["port"])
        self.token = c["token"]
        self.ssh = c["ssh"]
        self.remote_dir = c["remote_dir"]

        self.client = Machine("client", self.raw["client"])
        self.server = Machine("server", self.raw["server"])

        n = self.raw["network"]
        self.net = dict(n)
        self.prefix = int(n["prefix"])
        self.vni = int(n["vni"])
        self.bond_mode = int(n["bond_mode"])
        self.bond_xmit_policy = n["bond_xmit_policy"]

        r = self.raw["run"]
        self.topologies = split_words(r["topologies"])
        self.vxlan_modes = [_yes(v) for v in split_words(r["vxlan"])]
        self.duration = int(r["duration"])
        self.ramp = int(r["ramp"])
        self.settle = int(r["settle"])
        self.cooldown = int(r["cooldown"])
        self.optimum_tolerance = float(r["optimum_tolerance"]) / 100.0
        self.restore_bindings = _yes(r["restore_bindings"])

        s1 = self.raw["scenario1"]
        self.s1 = {
            "tests": split_words(s1["tests"]),
            "cores": [int(x) for x in split_words(s1["cores"])],
            "jumbo": [_yes(v) for v in split_words(s1["jumbo"])],
            "tps_payload": int(s1["tps_payload"]),
            "tps_cc_per_core": parse_rate(s1["tps_cc_per_core"]),
            "pps_payload": int(s1["pps_payload"]),
            "pps_cc_per_core": parse_rate(s1["pps_cc_per_core"]),
            "cps_per_core": parse_rate(s1["cps_per_core"]),
            "cps_ramp": int(s1["cps_ramp"]),
            "bw_payload": parse_size(s1["bw_payload"]),
            "bw_cc_per_core": parse_rate(s1["bw_cc_per_core"]),
        }
        s2 = self.raw["scenario2"]
        self.s2 = {
            "stacks": split_words(s2["stacks"]),
            "tests": split_words(s2["tests"]),
            "workers": [int(x) for x in split_words(s2["workers"])],
            "client_cores": int(s2["client_cores"]),
            "small_body": parse_size(s2["small_body"]),
            "bw_size": [parse_size(x) for x in split_words(s2["bw_size"])],
            "rps_cc_per_worker": parse_rate(s2["rps_cc_per_worker"]),
            "bw_cc_per_worker": parse_rate(s2["bw_cc_per_worker"]),
            "cps_per_worker": parse_rate(s2["cps_per_worker"]),
            "cps_ramp": int(s2["cps_ramp"]),
            "fstack_tso": int(s2["fstack_tso"]),
            "fstack_pkt_tx_delay": int(s2["fstack_pkt_tx_delay"]),
            "fstack_sendspace": int(s2["fstack_sendspace"]),
            "fstack_recvspace": int(s2["fstack_recvspace"]),
            "kernel_sendfile": _yes(s2["kernel_sendfile"]),
            "nginx_output_buffers": s2["nginx_output_buffers"],
        }

    def _abs(self, p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.base, p))

    def validate(self, role=None):
        for t in self.topologies:
            if t not in TOPOLOGIES:
                die("[run] topologies: unknown %r (allowed: %s)" % (t, ", ".join(TOPOLOGIES)))
        for t in self.s1["tests"]:
            if t not in S1_TESTS:
                die("[scenario1] tests: unknown %r (allowed: %s)" % (t, ", ".join(S1_TESTS)))
        for t in self.s2["tests"]:
            if t not in S2_TESTS:
                die("[scenario2] tests: unknown %r (allowed: %s)" % (t, ", ".join(S2_TESTS)))
        for s in self.s2["stacks"]:
            if s not in STACKS:
                die("[scenario2] stacks: unknown %r (allowed: %s)" % (s, ", ".join(STACKS)))
        if self.s2["small_body"] > 3500:
            die("[scenario2] small_body must be <= 3500 bytes (nginx 'return' literal limit)")
        if self.bond_xmit_policy not in ("l2", "l23", "l34"):
            die("[network] bond_xmit_policy must be l2, l23 or l34")
        if self.bond_mode not in (2, 4):
            die("[network] bond_mode must be 2 (balance-xor) or 4 (802.3ad/LACP)")
        if self.duration <= self.settle + 3:
            die("[run] duration must be larger than settle + 3")
        for k in ("server_ip", "client_ip", "server_ip2", "client_ip2", "client_range",
                  "client_range2", "server_range", "server_range2", "inner_server_ip",
                  "inner_server_ip2"):
            try:
                ipaddress.IPv4Address(self.net[k])
            except ValueError:
                die("[network] %s: bad IPv4 address %r" % (k, self.net[k]))
        lo = [int(ipaddress.IPv4Address(self.net[k])) & 0xffff for k in ("client_range", "client_range2")]
        if abs(lo[0] - lo[1]) < 16384:
            die("[network] client_range and client_range2 must differ in the last two bytes by "
                ">= 16384 (dperf indexes sockets by the low 16 bits of the client IP), "
                "e.g. 10.210.0.1 and 10.211.128.1")
        if role in ("client", None):
            self.client.validate()
        if role in ("server", None):
            self.server.validate()
        need_two = [t for t in self.topologies if t in ("bond", "dual")]
        if need_two:
            for m in ((self.client,) if role == "client" else (self.server,) if role == "server"
                      else (self.client, self.server)):
                if len(m.pci) < 2:
                    die("[%s] topologies %s need 2 PCI ports in 'pci'" % (m.role, need_two))
        if role in ("client", None) and not self.server_addr:
            die("[control] server_addr (management IP of the server machine) is empty")

    def digest(self):
        """Hash of the parameters both sides must agree on."""
        h = hashlib.sha1()
        for sec in ("network", "run", "scenario1", "scenario2"):
            for k in sorted(self.raw[sec]):
                h.update(("%s.%s=%s;" % (sec, k, self.raw[sec][k])).encode())
        return h.hexdigest()[:12]

    def machine(self, role):
        return self.client if role == "client" else self.server


class IpPlan(object):
    """Addressing of the test network. Port index 0 = first port (or bond),
    port index 1 = second port in the 'dual' topology."""

    def __init__(self, cfg):
        n = cfg.net
        self.prefix = cfg.prefix
        self.server_ip = [n["server_ip"], n["server_ip2"]]
        self.client_ip = [n["client_ip"], n["client_ip2"]]
        self.client_range = [n["client_range"], n["client_range2"]]
        self.server_range = [n["server_range"], n["server_range2"]]
        self.inner_server_ip = [n["inner_server_ip"], n["inner_server_ip2"]]
        self.server_inner_mac = n["server_inner_mac"]
        self.client_inner_mac = n["client_inner_mac"]
        self.vni = [cfg.vni, cfg.vni + 1]

    @staticmethod
    def add(ip, k):
        return str(ipaddress.IPv4Address(ip) + k)

    def netmask(self):
        return str(ipaddress.IPv4Network("0.0.0.0/%d" % self.prefix).netmask)

    def broadcast(self, port):
        net = ipaddress.IPv4Network("%s/%d" % (self.server_ip[port], self.prefix), strict=False)
        return str(net.broadcast_address)

    @staticmethod
    def net16(ip):
        return str(ipaddress.IPv4Network("%s/16" % ip, strict=False))

    @staticmethod
    def net24(ip):
        return str(ipaddress.IPv4Network("%s/24" % ip, strict=False))

    def vtep_base(self, side, port):
        """dperf-dperf VXLAN: per-core VTEPs inside the underlay subnet,
        server: x.y.1.1.., client: x.y.2.1.."""
        base = ipaddress.IPv4Address(self.server_ip[port])
        net = ipaddress.IPv4Network("%s/16" % base, strict=False)
        third = 1 if side == "server" else 2
        return str(net.network_address + third * 256 + 1)

    def inner_gw(self, port):
        """Fake next hop on the server's VXLAN interface; resolved statically
        to the dperf client's inner MAC."""
        net = ipaddress.IPv4Network("%s/24" % self.inner_server_ip[port], strict=False)
        return str(net.network_address + 254)

    def as_dict(self):
        return dict(self.__dict__)
