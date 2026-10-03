"""Linux kernel network configuration of the server for the kernel-stack nginx
tests: single port / LACP bond / two ports, optional VXLAN."""

import os
import re
import time

from .util import debug, info, read_file, run, wait_until, warn

BOND = "fbbond0"


def vxlan_dev(idx):
    return "fbvx%d" % idx


def ip(*args, **kw):
    return run(["ip"] + list(args), check=kw.get("check", True), quiet=kw.get("quiet", False))


class KernelNet(object):
    def __init__(self, plan, ifnames, bond_mode, bond_policy):
        self.plan = plan
        self.ifnames = ifnames      # physical interfaces (1 or 2)
        self.bond_mode = bond_mode
        self.bond_policy = bond_policy
        self.created = []           # links to delete on teardown
        self.routes = []            # (args...) to delete
        self.addrs = []             # (dev, cidr)
        self.underlay = []          # underlay device per port index

    def _reset_phys(self, dev, mtu=1500):
        ip("link", "set", dev, "down", check=False, quiet=True)
        ip("addr", "flush", "dev", dev, check=False, quiet=True)
        ip("link", "set", dev, "nomaster", check=False, quiet=True)
        ip("link", "set", dev, "mtu", str(mtu), check=False, quiet=True)

    def setup(self, topology, vxlan, nports_client_vtep):
        self.teardown(quiet=True)
        p = self.plan
        for dev in self.ifnames:
            self._reset_phys(dev)
        if topology == "bond":
            run(["modprobe", "bonding"], check=False, quiet=True)
            mode = "802.3ad" if self.bond_mode == 4 else "balance-xor"
            pol = {"l2": "layer2", "l23": "layer2+3", "l34": "layer3+4"}[self.bond_policy]
            args = ["link", "add", BOND, "type", "bond", "mode", mode, "miimon", "100",
                    "xmit_hash_policy", pol]
            if self.bond_mode == 4:
                args += ["lacp_rate", "fast"]
            ip(*args)
            self.created.append(BOND)
            for dev in self.ifnames[:2]:
                ip("link", "set", dev, "master", BOND)
            ip("link", "set", BOND, "up")
            self.underlay = [BOND]
        elif topology == "dual":
            self.underlay = list(self.ifnames[:2])
        else:
            self.underlay = [self.ifnames[0]]
        for dev in self.ifnames[:2 if topology in ("bond", "dual") else 1]:
            ip("link", "set", dev, "up")
        for idx, dev in enumerate(self.underlay):
            ip("link", "set", dev, "up")
            cidr = "%s/%d" % (p.server_ip[idx], p.prefix)
            ip("addr", "add", cidr, "dev", dev)
            self.addrs.append((dev, cidr))
        if topology == "bond":
            self.wait_bond()
        for idx, dev in enumerate(self.underlay):
            if vxlan:
                vx = vxlan_dev(idx)
                ip("link", "add", vx, "address", p.server_inner_mac, "type", "vxlan",
                   "id", str(p.vni[idx]), "local", p.server_ip[idx],
                   "remote", p.vtep_base("client", idx), "dev", dev, "dstport", "4789")
                self.created.append(vx)
                ip("link", "set", vx, "up")
                ip("addr", "add", "%s/24" % p.inner_server_ip[idx], "dev", vx)
                gw = p.inner_gw(idx)
                ip("neigh", "replace", gw, "lladdr", p.client_inner_mac, "dev", vx, "nud",
                   "permanent")
                self._route("replace", p.net16(p.client_range[idx]), "via", gw, "dev", vx)
            else:
                self._route("replace", p.net16(p.client_range[idx]), "via", p.client_ip[idx],
                            "dev", dev)
                # AnyIP: the whole FDIR server range is local (one IP per dperf client core)
                self._route("replace", "local", p.net24(p.server_range[idx]), "dev", "lo",
                            "table", "local")
        return self.underlay

    def _route(self, *args):
        ip("route", *args)
        self.routes.append([a for a in args[1:]])

    def wait_bond(self, timeout=40):
        path = "/proc/net/bonding/%s" % BOND

        def ready():
            txt = read_file(path, "") or ""
            if self.bond_mode != 4:
                return txt.count("MII Status: up") >= 3
            m = re.search(r"Active Aggregator Info:.*?Number of ports:\s*(\d+)", txt, re.S)
            return bool(m) and int(m.group(1)) >= 2
        if wait_until(ready, timeout, interval=1.0, what="bond"):
            info("%s is up with 2 active ports" % BOND)
        else:
            warn("%s: LACP did not aggregate both ports in %ds (check the peer side):\n%s"
                 % (BOND, timeout, (read_file(path, "") or "")[-1200:]))

    def teardown(self, quiet=False):
        for args in reversed(self.routes):
            ip("route", "del", *args, check=False, quiet=True)
        self.routes = []
        for dev, cidr in self.addrs:
            ip("addr", "del", cidr, "dev", dev, check=False, quiet=True)
        self.addrs = []
        for link in reversed(self.created):
            ip("link", "del", link, check=False, quiet=True)
        self.created = []
        # leftovers from an interrupted run
        for link in (vxlan_dev(0), vxlan_dev(1), BOND):
            if os.path.exists("/sys/class/net/%s" % link):
                ip("link", "del", link, check=False, quiet=True)
        p = self.plan
        for idx in (0, 1):
            ip("route", "del", "local", p.net24(p.server_range[idx]), "dev", "lo", "table", "local",
               check=False, quiet=True)
        for dev in self.ifnames:
            if os.path.exists("/sys/class/net/%s" % dev):
                ip("addr", "flush", "dev", dev, check=False, quiet=True)
