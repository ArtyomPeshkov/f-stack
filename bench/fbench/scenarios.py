"""Test matrix and execution.

Scenario 1: dperf client <-> dperf server  (maximum dperf can do, core sweep)
Scenario 2: dperf client  -> nginx         (kernel stack vs F-Stack, worker sweep)
"""

import json
import math
import os
import time

from . import dperf as dp
from . import host as hostmod
from . import nic as nicmod
from .config import IpPlan
from .util import BenchError, die, info, size_label, warn, write_file

KEEPALIVE_CLIENT = "1ms"
KEEPALIVE_SERVER = "1s"
CLIENT_BOUND_PCT = 90


def nports_of(topology):
    return 2 if topology == "dual" else 1


def phys_ports_of(topology):
    return 2 if topology in ("bond", "dual") else 1


def port_dev(cfg, machine, topology, idx):
    if topology == "bond":
        return dp.bond_spec(machine.pci[:2], cfg.bond_mode, cfg.bond_xmit_policy)
    return machine.pci[idx]


def max_payload(vxlan, jumbo):
    pkt = (dp.JUMBO_MTU + 14) if jumbo else 1514
    return pkt - (14 + 20 + 20) - (dp.VXLAN_HDR if vxlan else 0)


def point_id(p):
    if p["scenario"] == 1:
        parts = ["s1", p["topology"], "vxlan" if p["vxlan"] else "plain"]
        if p.get("jumbo"):
            parts.append("jumbo")
        parts += [p["test"], "c%d" % p["n"]]
    else:
        parts = ["s2", p["stack"], p["topology"], "vxlan" if p["vxlan"] else "plain", p["test"]]
        if p["test"] == "bw":
            parts.append(size_label(p["size"]))
        parts.append("w%d" % p["workers"])
    return "-".join(parts)


def client_base(plan, point, idx):
    """Client source range of port idx, shifted by whole /24 blocks per run so that
    late packets of the previous run's connections never match the new run's
    sockets (dperf picks client IP/ports deterministically)."""
    return plan.add(plan.client_range[idx], 256 * point.get("ipshift", 0))


def need_client_ips(cc_per_worker, cps_per_worker, ports_per_ip):
    need = max(cc_per_worker * 1.1, cps_per_worker * dp.RTO_SEC * 1.2, 1.0)
    return max(1, int(math.ceil(need / float(ports_per_ip))))


# =================================================================== plans
def s1_points(cfg, o):
    pts = []
    maxn = min(len(cfg.client.cores), len(cfg.server.cores))
    for topo in o["topologies"]:
        for vx in o["vxlan"]:
            for jumbo in o["jumbo"]:
                for test in o["s1_tests"]:
                    if jumbo and test in ("pps", "cps"):
                        continue
                    for n in o["s1_cores"]:
                        if n > maxn:
                            continue
                        if topo == "dual" and n % 2:
                            continue
                        pts.append({"scenario": 1, "topology": topo, "vxlan": vx, "jumbo": jumbo,
                                    "test": test, "n": n})
    return pts


def s2_points(cfg, o):
    pts = []
    order = [t for t in ("rps", "bw", "cps") if t in o["s2_tests"]]
    for stack in o["stacks"]:
        for topo in o["topologies"]:
            for vx in o["vxlan"]:
                for w in o["workers"]:
                    if w > len(cfg.server.cores):
                        continue
                    for test in order:
                        sizes = cfg.s2["bw_size"] if test == "bw" else [cfg.s2["small_body"]]
                        for size in sizes:
                            pts.append({"scenario": 2, "stack": stack, "topology": topo,
                                        "vxlan": vx, "test": test, "workers": w, "size": size})
    return pts


def estimate_seconds(cfg, o, pts):
    total = 0
    for p in pts:
        ramp = cfg.s1["cps_ramp"] if p["test"] == "cps" and p["scenario"] == 1 else \
            cfg.s2["cps_ramp"] if p["test"] == "cps" else o["ramp"]
        one = dp.WAIT_SEC + ramp + o["duration"] + dp.DRAIN_SEC + 12
        if p["test"] == "cps":
            one += dp.WAIT_SEC + ramp + 5 + dp.DRAIN_SEC
        total += one + o["cooldown"]
    return total


# ========================================================== scenario 1
def s1_layout(cfg, point, flow):
    topo, vx, n, test = point["topology"], point["vxlan"], point["n"], point["test"]
    nports = nports_of(topo)
    q = n // nports
    s1 = cfg.s1
    lay = {"nports": nports, "q": q}
    lay["server_ips"] = q if (vx or flow == "fdir") else 1
    ports_per_ip = dp.LPORTS if (flow == "fdir" or q == 1) else dp.LPORTS // q
    if test == "cps":
        cps_w = point.get("cps_target", s1["cps_per_core"] * n) / float(n)
        k = need_client_ips(0, cps_w, ports_per_ip)
    elif test == "pps":
        k = need_client_ips(s1["pps_cc_per_core"], s1["pps_cc_per_core"] / 4.0, ports_per_ip)
    elif test == "bw":
        k = need_client_ips(s1["bw_cc_per_core"], s1["bw_cc_per_core"], ports_per_ip)
    else:
        k = need_client_ips(s1["tps_cc_per_core"], s1["tps_cc_per_core"] / 2.0, ports_per_ip)
    lay["client_ips"] = k
    # dperf memory: client sockets = own IPs, server sockets = all client IPs of all ports
    lay["mem_client_mb"] = dp.memory_mb(n, k * dp.LPORTS, point.get("jumbo"))
    lay["mem_server_mb"] = dp.memory_mb(n, k * nports * dp.LPORTS * (1 if flow == "fdir" else
                                                                       lay["server_ips"]),
                                        point.get("jumbo"))
    return lay


def s1_cps_cap(cfg, point, flow):
    """Largest CPS target per core that fits into the hugepage budget."""
    n = point["n"]
    budget = min(cfg.client.hugepages_gb, cfg.server.hugepages_gb) * 1024 * 0.9
    target = cfg.s1["cps_per_core"]
    while target > 50000:
        p = dict(point, cps_target=target * n)
        lay = s1_layout(cfg, p, flow)
        if max(lay["mem_client_mb"], lay["mem_server_mb"]) <= budget:
            return target
        target = int(target * 0.8)
    return target


def s1_dperf_conf(cfg, role, point, flow, plan):
    m = cfg.machine(role)
    lay = s1_layout(cfg, point, flow)
    topo, vx, n, test, jumbo = (point["topology"], point["vxlan"], point["n"], point["test"],
                                point.get("jumbo"))
    if n > len(m.cores):
        die("[%s] has %d cores, test needs %d" % (role, len(m.cores), n))
    s1 = cfg.s1
    ports = []
    for idx in range(lay["nports"]):
        dev = port_dev(cfg, m, topo, idx)
        if role == "client":
            local, gw = plan.client_ip[idx], plan.server_ip[idx]
        else:
            local, gw = plan.server_ip[idx], plan.client_ip[idx]
        vxd = None
        if vx:
            peer = "server" if role == "client" else "client"
            smac, dmac = ((plan.client_inner_mac, plan.server_inner_mac) if role == "client"
                          else (plan.server_inner_mac, plan.client_inner_mac))
            vxd = {"vni": plan.vni[idx], "smac": smac, "dmac": dmac,
                   "local": plan.vtep_base(role, idx), "local_num": lay["q"],
                   "remote": plan.vtep_base(peer, idx), "remote_num": lay["q"]}
        ports.append(dp.Port(dev, local, gw, client_base(plan, point, idx), lay["client_ips"],
                             plan.server_range[idx], lay["server_ips"], vxd))
    client = role == "client"
    pmax = max_payload(vx, jumbo)
    opts = [("tx_burst", 128)]
    if client:
        ramp = point.get("ramp", cfg.s1["cps_ramp"] if test == "cps" else cfg.ramp)
        opts += [("launch_num", 10), ("duration", "%ds" % point.get("duration", cfg.duration)),
                 ("slow_start", ramp)]
    else:
        opts += [("duration", "1d")]
    if test == "tps":
        payload = min(s1["tps_payload"], pmax) if not jumbo else pmax - 60
        opts.append(("payload_size", payload))
        if client:
            cc = s1["tps_cc_per_core"] * n
            opts += [("cc", cc), ("cps", max(1000, cc // 2)), ("keepalive", KEEPALIVE_CLIENT)]
        else:
            opts.append(("keepalive", KEEPALIVE_SERVER))
    elif test == "pps":
        opts += [("protocol", "udp"), ("payload_size", s1["pps_payload"])]
        if client:
            cc = s1["pps_cc_per_core"] * n
            opts += [("flood", True), ("cc", cc), ("cps", max(1000, cc // 4)),
                     ("keepalive", KEEPALIVE_CLIENT)]
    elif test == "cps":
        opts.append(("payload_size", 1))
        if client:
            opts.append(("cps", int(point.get("cps_target", s1["cps_per_core"] * n))))
    elif test == "bw":
        opts.append(("protocol", "http"))
        if client:
            cc = s1["bw_cc_per_core"] * n
            opts += [("cc", cc), ("cps", max(1000, cc // 2)), ("keepalive", KEEPALIVE_CLIENT)]
        else:
            opts += [("payload_size", s1["bw_payload"]), ("send_window", 16),
                     ("keepalive", KEEPALIVE_SERVER)]
    if jumbo:
        opts.append(("jumbo", dp.JUMBO_MTU))
    if vx:
        opts.append(("mss", pmax))
    if flow == "rss" and lay["q"] > 1 and not vx:
        opts.append(("rss", True))
    comment = "fbench %s: %s side, flow=%s" % (point_id(point), role, flow)
    return dp.gen_conf("client" if client else "server", m.cores[:n], ports, opts, comment)


# ========================================================== scenario 2
def s2_client_cores(cfg, point):
    nports = nports_of(point["topology"])
    if point["vxlan"]:
        # the kernel/FreeBSD VXLAN device answers to a single remote VTEP,
        # dperf steers queues by VTEP => one client core per port
        return nports
    nc = min(cfg.s2["client_cores"], len(cfg.client.cores))
    return max(nports, nc - nc % nports)


def s2_layout(cfg, point, flow):
    nc = s2_client_cores(cfg, point)
    nports = nports_of(point["topology"])
    q = nc // nports
    w = point["workers"]
    s2 = cfg.s2
    lay = {"nc": nc, "nports": nports, "q": q}
    lay["server_ips"] = 1 if point["vxlan"] else (q if flow == "fdir" else 1)
    ports_per_ip = dp.LPORTS if (flow == "fdir" or q == 1) else dp.LPORTS // q
    test = point["test"]
    if test == "rps":
        cc = max(nc, s2["rps_cc_per_worker"] * w)
        lay.update(cc=cc, cps=max(1000, cc // 2))
    elif test == "bw":
        cc = max(nc, s2["bw_cc_per_worker"] * w)
        lay.update(cc=cc, cps=max(200, cc // 2))
    else:
        lay.update(cc=0, cps=int(point.get("cps_target", s2["cps_per_worker"] * w)))
    k = need_client_ips(lay["cc"] / float(nc), lay["cps"] / float(nc), ports_per_ip)
    budget_mb = cfg.client.hugepages_gb * 1024 * 0.9
    while k > 1 and dp.memory_mb(nc, k * dp.LPORTS, False) > budget_mb:
        k -= 1
    lay["client_ips"] = k
    lay["cps_cap"] = int(k * ports_per_ip / (dp.RTO_SEC * 1.2) * nc)
    lay["mem_client_mb"] = dp.memory_mb(nc, k * dp.LPORTS, False)
    return lay


def s2_client_conf(cfg, point, flow, plan):
    m = cfg.client
    lay = s2_layout(cfg, point, flow)
    topo, vx, test = point["topology"], point["vxlan"], point["test"]
    ports = []
    for idx in range(lay["nports"]):
        vxd = None
        if vx:
            dmacs = point.get("inner_macs") or [plan.server_inner_mac] * 2
            vxd = {"vni": plan.vni[idx], "smac": plan.client_inner_mac,
                   "dmac": dmacs[idx], "local": plan.vtep_base("client", idx),
                   "local_num": 1, "remote": plan.server_ip[idx], "remote_num": 1}
            server_base = plan.inner_server_ip[idx]
        else:
            server_base = plan.server_range[idx]
        ports.append(dp.Port(port_dev(cfg, m, topo, idx), plan.client_ip[idx], plan.server_ip[idx],
                             client_base(plan, point, idx), lay["client_ips"], server_base,
                             lay["server_ips"], vxd))
    from .nginx import SMALL_PATH, bw_path
    path = bw_path(point["size"]) if test == "bw" else SMALL_PATH
    ramp = point.get("ramp", cfg.s2["cps_ramp"] if test == "cps" else cfg.ramp)
    opts = [("tx_burst", 128), ("launch_num", 10), ("protocol", "http"), ("http_host", "fbench"),
            ("http_path", path), ("duration", "%ds" % point.get("duration", cfg.duration)),
            ("slow_start", ramp), ("cps", min(lay["cps"], lay["cps_cap"]) if test == "cps"
                                   else lay["cps"])]
    if lay["cc"]:
        opts += [("cc", lay["cc"]), ("keepalive", KEEPALIVE_CLIENT)]
    if vx:
        opts.append(("mss", max_payload(True, False)))
    if flow == "rss" and lay["q"] > 1 and not vx:
        opts.append(("rss", True))
    comment = "fbench %s: client side, flow=%s" % (point_id(point), flow)
    return dp.gen_conf("client", m.cores[:lay["nc"]], ports, opts, comment)


# ======================================================== results store
class Store(object):
    def __init__(self, cfg, run_name):
        self.dir = os.path.join(cfg.results_dir, run_name)
        os.makedirs(os.path.join(self.dir, "points"), exist_ok=True)
        self.path = os.path.join(self.dir, "results.jsonl")

    def point_dir(self, pid):
        d = os.path.join(self.dir, "points", pid)
        os.makedirs(d, exist_ok=True)
        return d

    def add(self, rec):
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def meta(self, data):
        write_file(os.path.join(self.dir, "run.json"), json.dumps(data, indent=2, default=str))


# ============================================================== runner
class Runner(object):
    def __init__(self, cfg, opts, agent, store):
        self.cfg = cfg
        self.o = opts
        self.agent = agent
        self.store = store
        self.plan = IpPlan(cfg)
        self.flow = opts.get("flow", "fdir")
        self.work = os.path.join(cfg.work_dir, "client")
        os.makedirs(self.work, exist_ok=True)
        self.done = 0
        self.total = 0
        self.seq = 0

    # ------------------------------------------------------------ helpers
    def _with_shift(self, p, client_ips):
        span = (client_ips + 1 + 255) // 256
        slots = max(1, 127 // span)
        self.seq += 1
        return dict(p, ipshift=(self.seq % slots) * span)

    def _progress(self, p):
        self.done += 1
        return "[%d/%d] %s" % (self.done, self.total, point_id(p))

    def _client_run(self, p, conf_text, pdir, tag, fail_key, server_cores, fstack_procs=0,
                    win_first=None):
        conf = os.path.join(pdir, "dperf-client%s.conf" % tag)
        write_file(conf, conf_text)
        dp.validate_conf(self.cfg.dperf_bin, conf)
        S = p.get("ramp", self.cfg.ramp)
        D = p.get("duration", self.cfg.duration)
        state = {"win": None, "err": None}

        def wstart():
            try:
                self.agent.call("window_start", cores=server_cores, fstack_procs=fstack_procs)
            except BenchError as e:
                state["err"] = str(e)

        def wstop():
            try:
                state["win"] = self.agent.call("window_stop")
            except BenchError as e:
                state["err"] = str(e)
        crun = dp.ClientRun(self.cfg.dperf_bin, conf, os.path.join(pdir, "dperf-client%s.log" % tag),
                            S, D, self.cfg.settle, wstart, wstop, fail_fast_key=fail_key)
        if win_first is not None:
            crun.win_first = win_first
        crun.run()
        if crun.t_window_start and not crun.t_window_stop:
            try:
                state["win"] = self.agent.call("window_stop")
            except BenchError:
                pass
        if crun.parser.port_error == "fdir":
            raise BenchError("FDIR-UNSUPPORTED on the client NIC")
        if crun.aborted:
            raise BenchError("dperf client aborted: %s" % crun.aborted)
        if not crun.parser.blocks:
            raise BenchError("dperf client produced no statistics:\n%s" % crun.proc.tail(25))
        if state["err"]:
            warn("window measurement error: %s" % state["err"])
        return crun, state["win"] or {}

    @staticmethod
    def _cpu_in_range(win, t_from, t_to):
        """Average agent CPU series entries within client-relative offsets."""
        series = (win.get("cpu") or {}).get("series") or []
        sel = [s for s in series if t_from - 0.5 <= s["t"] <= t_to + 0.5]
        if not sel:
            return None
        return sum(s["busy_cores_total"] for s in sel) / float(len(sel))

    def _flags(self, csum, metric_tx):
        flags = []
        if csum.get("cpu_max_worker", 0) >= CLIENT_BOUND_PCT:
            flags.append("client-bound")
        if csum.get("seconds") and csum.get("rsp", 0) < 1 and csum.get("udpRx", 0) < 1:
            flags.append("stalled")
        bad = csum.get("errors", 0) + csum.get("retrans", 0) + csum.get("imissed", 0)
        if metric_tx and bad > 0.001 * metric_tx * csum.get("seconds", 1):
            flags.append("errors")
        return flags

    def _diag(self, pdir):
        try:
            d = self.agent.call("diag")
            write_file(os.path.join(pdir, "server-diag.json"), json.dumps(d, indent=2))
        except BenchError:
            pass

    # ------------------------------------------------------------ scenario 1
    def run_s1(self, points):
        for p in points:
            label = self._progress(p)
            pid = point_id(p)
            pdir = self.store.point_dir(pid)
            rec = dict(p, id=pid, started=time.time())
            try:
                if p["vxlan"] and p["n"] // nports_of(p["topology"]) > 1 and self.flow == "rss":
                    raise BenchError("multi-core VXLAN needs FDIR (rte_flow) support on the NIC")
                if p["test"] == "cps":
                    self._s1_cps(p, pdir, rec)
                else:
                    self._s1_once(p, pdir, rec, "")
                rec["status"] = "ok"
                m = rec["metric"]
                info("%s -> %s %s %s" % (label, fmt_metric(m["value"], m["unit"]), m["unit"],
                                         " ".join("[%s]" % f for f in rec.get("flags", []))))
            except BenchError as e:
                msg = str(e)
                if "FDIR-UNSUPPORTED" in msg and self.flow == "fdir":
                    warn("NIC does not support FDIR rules, switching dperf to 'rss' mode")
                    self.flow = "rss"
                    try:
                        if p["test"] == "cps":
                            self._s1_cps(p, pdir, rec)
                        else:
                            self._s1_once(p, pdir, rec, "")
                        rec["status"] = "ok"
                        m = rec["metric"]
                        info("%s -> %s %s" % (label, fmt_metric(m["value"], m["unit"]), m["unit"]))
                    except BenchError as e2:
                        rec.update(status="failed", error=str(e2)[-3000:])
                else:
                    rec.update(status="failed", error=msg[-3000:])
                if rec.get("status") == "failed":
                    warn("%s FAILED: %s" % (label, rec["error"].splitlines()[0] if rec["error"] else ""))
                    self._diag(pdir)
            finally:
                try:
                    self.agent.call("dperf_stop")
                except BenchError:
                    pass
            rec["flow"] = self.flow
            rec["finished"] = time.time()
            self.store.add(rec)
            time.sleep(self.cfg.cooldown)

    def _s1_once(self, p, pdir, rec, tag):
        p = self._with_shift(p, s1_layout(self.cfg, p, self.flow)["client_ips"])
        conf_c = s1_dperf_conf(self.cfg, "client", p, self.flow, self.plan)
        srv = self.agent.call("dperf_start", point=p, flow=self.flow)
        write_file(os.path.join(pdir, "dperf-server%s.conf" % tag), srv.get("conf", ""))
        fail_key = "udpRx" if p["test"] == "pps" else "rsp"
        win_first = dp.WAIT_SEC + 1 if p["test"] == "cps" else None
        try:
            crun, win = self._client_run(p, conf_c, pdir, tag, fail_key,
                                         self.cfg.server.cores[:p["n"]], win_first=win_first)
        finally:
            stop = self.agent.call("dperf_stop")
            write_file(os.path.join(pdir, "dperf-server%s.tail" % tag), (stop or {}).get("log_tail", ""))
        csum = dp.summarize(crun.window_blocks(), crun.prev_block())
        ssum = win.get("dperf_server") or {}
        lay = s1_layout(self.cfg, p, self.flow)
        rec["layout"] = lay
        rec["client"] = csum
        rec["server"] = {"dperf": ssum, "cpu": strip_series(win.get("cpu"))}
        test = p["test"]
        if test == "tps":
            val, unit = csum["gbps_rx"] + csum["gbps_tx"], "Gbps"
            rec["extra"] = {"rps": csum["rsp"], "gbps_rx": csum["gbps_rx"],
                            "gbps_tx": csum["gbps_tx"], "mpps_rx": csum["mpps_rx"],
                            "mpps_tx": csum["mpps_tx"]}
            tx = csum["req"]
        elif test == "bw":
            val, unit = csum["gbps_rx"], "Gbps"
            rec["extra"] = {"rps": csum["rsp"], "mpps_rx": csum["mpps_rx"]}
            tx = csum["req"]
        elif test == "pps":
            srx = ssum.get("pktRx", 0) / 1e6 if ssum.get("seconds") else 0.0
            val, unit = srx or csum["mpps_rx"], "Mpps"
            rec["extra"] = {"client_mpps_tx": csum["mpps_tx"], "client_mpps_rx": csum["mpps_rx"],
                            "server_mpps_rx": srx, "server_mpps_tx": ssum.get("pktTx", 0) / 1e6}
            tx = csum["pktTx"]
        else:
            blocks = [b for b in crun.parser.blocks if dp.WAIT_SEC < b["sec"] <= crun.win_last]
            peak, at = dp.best_window(blocks, "rsp", 5)
            steady = dp.summarize([b for b in crun.parser.blocks
                                   if p.get("ramp", 0) + self.cfg.settle <= b["sec"] <= crun.win_last])
            val, unit = peak, "conn/s"
            rec["extra"] = {"cps_peak": peak, "cps_steady": steady.get("rsp", 0),
                            "cps_target": p.get("cps_target")}
            tx = steady.get("req", 0)
        rec["metric"] = {"name": test, "value": val, "unit": unit}
        if test == "cps":
            # judge errors at the peak, not in the deliberately overloaded steady phase
            csum_err = dp.summarize(blocks[at:at + 5])
            tx = csum_err.get("req", 0)
        else:
            csum_err = csum
        sc = ssum.get("cpu_avg")
        rec["cpu"] = {"client_dperf_pct": csum.get("cpu_avg"), "server_dperf_pct": sc,
                      "client_max_worker_pct": csum.get("cpu_max_worker"),
                      "server_max_worker_pct": ssum.get("cpu_max_worker")}
        rec["flags"] = self._flags(csum_err, tx)
        if ssum.get("cpu_max_worker", 0) >= CLIENT_BOUND_PCT and test != "pps":
            rec["flags"].append("server-busy")

    def _s1_cps(self, p, pdir, rec):
        n = p["n"]
        cap = s1_cps_cap(self.cfg, p, self.flow)
        if cap < self.cfg.s1["cps_per_core"]:
            warn("CPS target limited to %s/core by the hugepage budget" % fmt_metric(cap, ""))
        first = dict(p, cps_target=cap * n, duration=5, ramp=self.cfg.s1["cps_ramp"])
        self._s1_once(first, pdir, rec, ".pass1")
        peak1 = rec["metric"]["value"]
        if peak1 >= 0.9 * cap * n:
            rec.setdefault("flags", []).append("unsaturated")
            return
        time.sleep(self.cfg.cooldown)
        second = dict(p, cps_target=int(peak1 * 1.25), duration=self.cfg.duration,
                      ramp=self.cfg.s1["cps_ramp"])
        rec2 = dict(rec)
        self._s1_once(second, pdir, rec2, ".pass2")
        if rec2["metric"]["value"] >= peak1 * 0.9:
            rec.update(rec2)
        rec["extra"]["pass1_peak"] = peak1

    # ------------------------------------------------------------ scenario 2
    def run_s2(self, points):
        groups = []
        for p in points:
            key = (p["stack"], p["topology"], p["vxlan"], p["workers"])
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(p)
        for key, pts in groups:
            stack, topo, vx, w = key
            gp = dict(pts[0])
            lay = s2_layout(self.cfg, gp, self.flow)
            gp["server_ips_per_port"] = lay["server_ips"]
            started = None
            try:
                started = self.agent.call("s2_start", point=gp, timeout=1800)
            except BenchError as e:
                for p in pts:
                    label = self._progress(p)
                    pid = point_id(p)
                    warn("%s FAILED (server setup): %s" % (label, str(e).splitlines()[0]))
                    pdir = self.store.point_dir(pid)
                    self._diag(pdir)
                    self.store.add(dict(p, id=pid, status="failed", error=str(e)[-3000:],
                                        started=time.time(), finished=time.time()))
                continue
            gdir = self.store.point_dir("s2-%s-%s-%s-w%d" % (stack, topo, "vxlan" if vx else "plain", w))
            for name in ("nginx_conf", "fstack_conf"):
                if started.get(name):
                    write_file(os.path.join(gdir, name.replace("_", ".")), started[name])
            write_file(os.path.join(gdir, "server-setup.json"), json.dumps(started, indent=2))
            try:
                for p in pts:
                    if started.get("inner_macs"):
                        p = dict(p, inner_macs=started["inner_macs"])
                    self._s2_point(p)
                    time.sleep(self.cfg.cooldown)
            finally:
                try:
                    stop = self.agent.call("s2_stop")
                    write_file(os.path.join(gdir, "nginx-error.log.tail"),
                               (stop or {}).get("error_log_tail", ""))
                except BenchError as e:
                    warn("s2_stop: %s" % e)

    def _s2_point(self, p):
        label = self._progress(p)
        pid = point_id(p)
        pdir = self.store.point_dir(pid)
        rec = dict(p, id=pid, started=time.time())
        try:
            try:
                if p["test"] == "cps":
                    self._s2_cps(p, pdir, rec)
                else:
                    self._s2_once(p, pdir, rec, "")
            except BenchError as e:
                if "FDIR-UNSUPPORTED" in str(e) and self.flow == "fdir" and not p["vxlan"]:
                    warn("client NIC does not support FDIR rules, switching dperf to 'rss' mode")
                    self.flow = "rss"
                    if p["test"] == "cps":
                        self._s2_cps(p, pdir, rec)
                    else:
                        self._s2_once(p, pdir, rec, "")
                else:
                    raise
            rec["status"] = "ok"
            m = rec["metric"]
            info("%s -> %s %s, server CPU %.2f cores %s" % (
                label, fmt_metric(m["value"], m["unit"]), m["unit"],
                rec["cpu"].get("server_busy_cores") or 0.0,
                " ".join("[%s]" % f for f in rec.get("flags", []))))
        except BenchError as e:
            rec.update(status="failed", error=str(e)[-3000:])
            warn("%s FAILED: %s" % (label, str(e).splitlines()[0]))
            self._diag(pdir)
        rec["flow"] = self.flow
        rec["finished"] = time.time()
        self.store.add(rec)

    def _s2_once(self, p, pdir, rec, tag):
        p = self._with_shift(p, s2_layout(self.cfg, p, self.flow)["client_ips"])
        conf = s2_client_conf(self.cfg, p, self.flow, self.plan)
        fstack = p["workers"] if p["stack"] == "fstack" else 0
        win_first = dp.WAIT_SEC + 1 if p["test"] == "cps" else None
        crun, win = self._client_run(p, conf, pdir, tag, "rsp",
                                     self.cfg.server.cores[:p["workers"]], fstack_procs=fstack,
                                     win_first=win_first)
        csum = dp.summarize(crun.window_blocks(), crun.prev_block())
        lay = s2_layout(self.cfg, p, self.flow)
        rec["layout"] = lay
        rec["client"] = csum
        cpu = win.get("cpu") or {}
        top = win.get("fstack_top") or {}
        rec["server"] = {"cpu": strip_series(cpu), "fstack_top": strip_series(top)}
        test = p["test"]
        busy = cpu.get("busy_cores_total")
        if test == "cps":
            blocks = [b for b in crun.parser.blocks if dp.WAIT_SEC < b["sec"] <= crun.win_last]
            peak, at = dp.best_window(blocks, "rsp", 5)
            steady_blocks = [b for b in crun.parser.blocks
                             if p.get("ramp", 0) + self.cfg.settle <= b["sec"] <= crun.win_last]
            steady = dp.summarize(steady_blocks)
            val, unit = peak, "conn/s"
            if crun.t_window_start and blocks[at:at + 5]:
                t_from = blocks[at]["t"] - crun.t_window_start - 1
                t_to = blocks[min(at + 4, len(blocks) - 1)]["t"] - crun.t_window_start
                b2 = self._cpu_in_range(win, t_from, t_to)
                if b2 is not None:
                    busy = b2
            rec["extra"] = {"cps_peak": peak, "cps_steady": steady.get("rsp", 0),
                            "cps_target": p.get("cps_target"), "gbps_rx": csum["gbps_rx"]}
            tx = steady.get("req", 0)
        elif test == "bw":
            val, unit = csum["gbps_rx"], "Gbps"
            rec["extra"] = {"rps": csum["rsp"], "mpps_rx": csum["mpps_rx"]}
            tx = csum["req"]
        else:
            val, unit = csum["rsp"], "req/s"
            rec["extra"] = {"gbps_rx": csum["gbps_rx"], "gbps_tx": csum["gbps_tx"]}
            tx = csum["req"]
        rec["metric"] = {"name": test, "value": val, "unit": unit}
        csum_err = csum
        if test == "cps":
            csum_err = dp.summarize(blocks[at:at + 5])
            tx = csum_err.get("req", 0)
        rec["cpu"] = {
            "client_dperf_pct": csum.get("cpu_avg"),
            "client_max_worker_pct": csum.get("cpu_max_worker"),
            "server_busy_cores": busy,
            "server_busy_cores_selected": cpu.get("busy_cores_selected"),
            "server_softirq_cores": cpu.get("irq_cores"),
            "server_allocated_cores": p["workers"],
            "fstack_effective_cores": top.get("busy_cores"),
        }
        rec["flags"] = self._flags(csum_err, tx)
        if p["stack"] == "fstack" and not top.get("samples"):
            rec["flags"].append("no-ff_top")

    def _s2_cps(self, p, pdir, rec):
        w = p["workers"]
        target = self.cfg.s2["cps_per_worker"] * w
        first = dict(p, cps_target=target, duration=5, ramp=self.cfg.s2["cps_ramp"])
        cap = s2_layout(self.cfg, first, self.flow)["cps_cap"]
        if cap < target:
            warn("CPS target limited to %s by the client hugepage budget" % fmt_metric(cap, ""))
            first["cps_target"] = cap
        self._s2_once(first, pdir, rec, ".pass1")
        peak1 = rec["metric"]["value"]
        if peak1 >= 0.9 * first["cps_target"]:
            rec.setdefault("flags", []).append("unsaturated")
            return
        time.sleep(self.cfg.cooldown)
        second = dict(p, cps_target=int(peak1 * 1.25), duration=self.cfg.duration,
                      ramp=self.cfg.s2["cps_ramp"])
        rec2 = dict(rec)
        self._s2_once(second, pdir, rec2, ".pass2")
        if rec2["metric"]["value"] >= peak1 * 0.9:
            rec.update(rec2)
        rec["extra"]["pass1_peak"] = peak1


def strip_series(d):
    if not d:
        return d
    return {k: v for k, v in d.items() if k != "series"}


def fmt_metric(v, unit):
    if v is None:
        return "-"
    if unit in ("Gbps", "Mpps"):
        return "%.2f" % v
    return "%.0f" % v if v < 1000 else "%.3fM" % (v / 1e6) if v >= 1e6 else "%.1fk" % (v / 1e3)
