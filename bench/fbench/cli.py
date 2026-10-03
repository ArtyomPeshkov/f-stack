"""Command line interface of fbench."""

import argparse
import datetime
import json
import os
import signal
import sys
import time

from . import VERSION
from . import build as buildmod
from . import dperf as dp
from . import fstack as fs
from . import host as hostmod
from . import nginx as ngx
from . import nic as nicmod
from . import report as reportmod
from . import scenarios as sc
from . import util
from .config import Config, IpPlan, S1_TESTS, S2_TESTS, STACKS, TOPOLOGIES
from .util import BenchError, die, info, warn, write_file

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sigterm(signum, frame):
    raise KeyboardInterrupt()


def matrix_opts(cfg, a):
    def pick(val, default, allowed=None, conv=str):
        if not val:
            return default
        out = [conv(x) for x in util.split_words(val)]
        if allowed:
            for x in out:
                if x not in allowed:
                    die("unknown value %r (allowed: %s)" % (x, ", ".join(map(str, allowed))))
        return out

    def yesno(x):
        return x.lower() in ("on", "yes", "1", "true")
    o = {
        "topologies": pick(a.topologies, cfg.topologies, TOPOLOGIES),
        "vxlan": pick(a.vxlan, cfg.vxlan_modes, None, yesno),
        "jumbo": cfg.s1["jumbo"],
        "s1_tests": cfg.s1["tests"],
        "s2_tests": cfg.s2["tests"],
        "s1_cores": pick(a.cores, cfg.s1["cores"], None, int),
        "workers": pick(a.workers, cfg.s2["workers"], None, int),
        "stacks": pick(a.stacks, cfg.s2["stacks"], STACKS),
    }
    if a.tests:
        tests = util.split_words(a.tests)
        for t in tests:
            if t not in S1_TESTS and t not in S2_TESTS:
                die("unknown test %r (scenario 1: %s; scenario 2: %s)" %
                    (t, ", ".join(S1_TESTS), ", ".join(S2_TESTS)))
        o["s1_tests"] = [t for t in tests if t in S1_TESTS]
        o["s2_tests"] = [t for t in tests if t in S2_TESTS]
    if a.quick:
        cfg.duration = min(cfg.duration, 15)
        cfg.settle = min(cfg.settle, 3)
    if a.duration:
        cfg.duration = a.duration
    if a.ramp:
        cfg.ramp = a.ramp
    o["ramp"], o["duration"], o["cooldown"] = cfg.ramp, cfg.duration, cfg.cooldown
    o["flow"] = a.flow
    return o


def build_points(cfg, o, scenario):
    pts1 = sc.s1_points(cfg, o) if scenario in ("1", "all") else []
    pts2 = sc.s2_points(cfg, o) if scenario in ("2", "all") else []
    return pts1, pts2


def dperf_has_csum_bug(cfg):
    src = util.read_file(os.path.join(cfg.dperf_dir, "src", "tcp.c"), "") or ""
    return buildmod.DPERF_CSUM_BUG in src


def print_plan(cfg, o, pts1, pts2):
    eta = sc.estimate_seconds(cfg, o, pts1 + pts2)
    info("plan: scenario 1: %d points, scenario 2: %d points, estimated %s" %
         (len(pts1), len(pts2), str(datetime.timedelta(seconds=int(eta)))))
    if pts1:
        info("  S1 topologies=%s vxlan=%s tests=%s cores/side=%s" % (
            ",".join(o["topologies"]), ",".join("on" if v else "off" for v in o["vxlan"]),
            ",".join(o["s1_tests"]), ",".join(map(str, sorted(set(p["n"] for p in pts1))))))
    if pts2:
        info("  S2 stacks=%s topologies=%s vxlan=%s tests=%s workers=%s" % (
            ",".join(o["stacks"]), ",".join(o["topologies"]),
            ",".join("on" if v else "off" for v in o["vxlan"]), ",".join(o["s2_tests"]),
            ",".join(map(str, sorted(set(p["workers"] for p in pts2))))))


# ---------------------------------------------------------------- commands
def cmd_check(cfg, a):
    role = a.role
    cfg.validate(role)
    m = cfg.machine(role)
    ok = True
    info("fbench %s, role %s, config %s (digest %s)" % (VERSION, role, cfg.path, cfg.digest()))
    try:
        buildmod.build_env(cfg)
    except BenchError as e:
        warn(str(e))
        ok = False
    bins = [("dperf", cfg.dperf_bin)]
    if role == "server":
        bins += [("nginx (F-Stack)", cfg.nginx_fstack), ("nginx (kernel)", cfg.nginx_kernel),
                 ("ff_ifconfig", os.path.join(cfg.fstack_tools, "ifconfig")),
                 ("ff_top", os.path.join(cfg.fstack_tools, "top"))]
    for name, path in bins:
        if os.path.isfile(path):
            info("  %-16s %s" % (name, path))
        else:
            warn("  %-16s MISSING %s (run ./build.sh)" % (name, path))
            ok = False
    for st in nicmod.NicManager(m, cfg.fstack_dir, cfg.dpdk_build).status():
        info("  NIC %(pci)s driver=%(driver)s numa=%(numa)s netdev=%(netdev)s" % st)
    for w in hostmod.check_host(m):
        warn("  " + w)
    if dperf_has_csum_bug(cfg):
        warn("  dperf sources have the VXLAN inner TCP checksum bug: run ./build.sh to patch")
        ok = False
    node = nicmod.numa_node(m.pci[0])
    info("  free hugepages on node %d: %d MiB" % (node, hostmod.free_hugepages_mb(node)))
    return 0 if ok else 1


def cmd_build(cfg, a):
    buildmod.build_all(cfg, util.split_words(a.what), a.force)
    return 0


def cmd_agent(cfg, a):
    from .agent import Agent
    cfg.validate("server")
    util.require_root()
    ag = Agent(cfg)
    bind = a.bind if a.bind is not None else cfg.server_addr
    ag.serve(bind, cfg.port)
    return 0


def cmd_plan(cfg, a):
    o = matrix_opts(cfg, a)
    pts1, pts2 = build_points(cfg, o, a.scenario)
    print_plan(cfg, o, pts1, pts2)
    for p in pts1 + pts2:
        print("  " + sc.point_id(p))
    return 0


def cmd_gen(cfg, a):
    """Write every config the run would use, validate dperf configs."""
    cfg.validate(None)
    o = matrix_opts(cfg, a)
    pts1, pts2 = build_points(cfg, o, a.scenario)
    plan = IpPlan(cfg)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    have_dperf = os.path.isfile(cfg.dperf_bin)
    n_ok = 0
    for p in pts1:
        for role in ("client", "server"):
            path = os.path.join(out, "%s.%s.conf" % (sc.point_id(p), role))
            pp = dict(p)
            if p["test"] == "cps":
                pp["cps_target"] = sc.s1_cps_cap(cfg, p, o["flow"]) * p["n"]
            write_file(path, sc.s1_dperf_conf(cfg, role, pp, o["flow"], plan))
            if have_dperf:
                dp.validate_conf(cfg.dperf_bin, path)
                n_ok += 1
    seen = set()
    for p in pts2:
        pid = sc.point_id(p)
        pp = dict(p)
        path = os.path.join(out, "%s.client.conf" % pid)
        write_file(path, sc.s2_client_conf(cfg, pp, o["flow"], plan))
        if have_dperf:
            dp.validate_conf(cfg.dperf_bin, path)
            n_ok += 1
        key = (p["stack"], p["topology"], p["vxlan"], p["workers"])
        if key in seen:
            continue
        seen.add(key)
        lay = sc.s2_layout(cfg, p, o["flow"])
        cores = cfg.server.cores[:p["workers"]]
        gdir = os.path.join(out, "s2-%s-%s-%s-w%d" % (p["stack"], p["topology"],
                                                      "vxlan" if p["vxlan"] else "plain",
                                                      p["workers"]))
        os.makedirs(gdir, exist_ok=True)
        fpath = os.path.join(gdir, "f-stack.conf")
        if p["stack"] == "fstack":
            nports = sc.phys_ports_of(p["topology"])
            write_file(fpath, fs.gen_conf(cores, cfg.server.pci[:nports], p["topology"], plan,
                                          cfg.s2, cfg.bond_mode, cfg.bond_xmit_policy,
                                          0 if p["vxlan"] else lay["server_ips"]))
        write_file(os.path.join(gdir, "nginx.conf"),
                   ngx.gen_conf(p["stack"], gdir, p["workers"], cores, cfg.s2["small_body"], cfg.s2,
                                fstack_conf=fpath))
    info("configs written to %s (%d dperf configs validated%s)" %
         (out, n_ok, "" if have_dperf else "; dperf binary not found, validation skipped"))
    return 0


def cmd_report(cfg, a):
    path = reportmod.generate(os.path.abspath(a.run_dir), cfg.optimum_tolerance)
    info("report: %s" % path)
    return 0


def cmd_restore(cfg, a):
    util.require_root()
    m = cfg.machine(a.role)
    for b in (cfg.dperf_bin, cfg.nginx_fstack, cfg.nginx_kernel):
        util.kill_exe(b)
    if a.role == "server":
        from .kernelnet import KernelNet
        KernelNet(IpPlan(cfg), [], cfg.bond_mode, cfg.bond_xmit_policy).teardown()
    nm = nicmod.NicManager(m, cfg.fstack_dir, cfg.dpdk_build)
    names = nm.bind_kernel()
    info("ports are back on the kernel driver: %s" % ", ".join(names))
    return 0


def cmd_run(cfg, a):
    from .agent import AgentClient
    util.require_root()
    cfg.validate("client")
    if not os.path.isfile(cfg.dperf_bin):
        die("dperf not found at %s - run ./build.sh first" % cfg.dperf_bin)
    o = matrix_opts(cfg, a)
    pts1, pts2 = build_points(cfg, o, a.scenario)
    if not pts1 and not pts2:
        die("nothing to run (check the filters / core counts)")
    if any(p["vxlan"] for p in pts2) and dperf_has_csum_bug(cfg):
        die("dperf in %s still has the VXLAN inner TCP checksum bug: the server stacks would drop "
            "every packet. Run ./build.sh (it patches and rebuilds dperf)" % cfg.dperf_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = stamp + ("-" + a.name if a.name else "") + "-s%s" % a.scenario
    store = sc.Store(cfg, name)
    util.set_log_file(os.path.join(store.dir, "run.log"))
    info("fbench %s: results in %s" % (VERSION, store.dir))
    print_plan(cfg, o, pts1, pts2)

    power = hostmod.PowerSettings()
    nm = nicmod.NicManager(cfg.client, cfg.fstack_dir, cfg.dpdk_build)
    agent = AgentClient(cfg)
    restore = cfg.restore_bindings and not a.keep_bindings
    try:
        hp = hostmod.ensure_hugepages(cfg.client.hugepages_gb, nicmod.numa_node(cfg.client.pci[0]))
        power.apply()
        nm.bind_dpdk(cfg.client.pci[:2] if any(t in ("bond", "dual") for t in o["topologies"])
                     else cfg.client.pci[:1])
        agent.connect()
        hello = agent.call("hello", digest=cfg.digest(), version=VERSION)
        if not hello.get("digest_ok"):
            msg = ("bench.conf differs between the machines (client %s, server %s): copy the same "
                   "file to both" % (cfg.digest(), hello.get("digest")))
            if a.ignore_digest:
                warn(msg)
            else:
                die(msg + " (or use --ignore-digest)")
        if not hello.get("client_version_ok"):
            warn("fbench version differs on the server (%s): update both checkouts" %
                 hello.get("version"))
        server_info = agent.call("prepare", timeout=300)
        client_info = hostmod.host_info(cfg.client)
        client_info["hugepages"] = hp
        for role, hi in (("client", client_info), ("server", server_info)):
            for w in hi.get("warnings", []):
                warn("%s: %s" % (role, w))
        store.meta({"version": VERSION, "started": time.time(),
                    "started_h": datetime.datetime.now().isoformat(timespec="seconds"),
                    "digest": cfg.digest(), "options": o, "config": cfg.text,
                    "hosts": {"client": client_info, "server": server_info},
                    "argv": sys.argv})
        runner = sc.Runner(cfg, o, agent, store)
        runner.total = len(pts1) + len(pts2)
        if pts1:
            runner.run_s1(pts1)
        if pts2:
            runner.run_s2(pts2)
    except KeyboardInterrupt:
        warn("interrupted, cleaning up")
    finally:
        util.kill_all_procs()
        if agent.sock:
            try:
                agent.call("cleanup", restore_bindings=restore, timeout=600)
            except BenchError as e:
                warn("server cleanup: %s" % e)
            agent.close(shutdown=agent.ssh_started)
        if restore:
            try:
                nm.bind_kernel()
            except BenchError as e:
                warn("client rebind: %s" % e)
        power.restore()
        try:
            path = reportmod.generate(store.dir, cfg.optimum_tolerance)
            info("report: %s" % path)
        except Exception as e:  # report problems must not hide the run result
            warn("report generation failed: %s" % e)
    return 0


def main(argv=None):
    if not util.py_version_ok():
        print("python >= 3.6 required")
        return 2
    ap = argparse.ArgumentParser(prog="fbench.py", description="dperf / F-Stack / nginx stand")
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "bench.conf"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    def matrix(p):
        p.add_argument("--scenario", "-s", choices=("1", "2", "all"), default="all")
        p.add_argument("--topologies", help="single,bond,dual")
        p.add_argument("--vxlan", help="off,on")
        p.add_argument("--tests", help="S1: tps,pps,cps,bw  S2: rps,cps,bw")
        p.add_argument("--cores", help="S1 cores per side, e.g. 1,2,4,8")
        p.add_argument("--workers", help="S2 nginx workers, e.g. 1,2,4,8")
        p.add_argument("--stacks", help="S2 stacks: kernel,fstack")
        p.add_argument("--duration", type=int, help="steady seconds per point")
        p.add_argument("--ramp", type=int, help="dperf slow_start seconds")
        p.add_argument("--quick", action="store_true", help="short runs (15s)")
        p.add_argument("--flow", choices=("fdir", "rss"), default="fdir",
                       help="dperf queue steering (fdir falls back to rss automatically)")

    p = sub.add_parser("check", help="pre-flight checks on this machine")
    p.add_argument("--role", choices=("client", "server"), required=True)
    p = sub.add_parser("build", help="build dperf, libfstack, F-Stack tools, nginx x2")
    p.add_argument("--what", default="all", help="all | dperf,fstack,tools,nginx")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("agent", help="run the server-side agent")
    p.add_argument("--bind", help="listen address (default: [control] server_addr)")
    p = sub.add_parser("run", help="run benchmarks (on the client machine)")
    matrix(p)
    p.add_argument("--name", help="suffix for the results directory")
    p.add_argument("--keep-bindings", action="store_true", help="leave NICs bound to DPDK")
    p.add_argument("--ignore-digest", action="store_true")
    p = sub.add_parser("plan", help="print the test matrix and its duration")
    matrix(p)
    p = sub.add_parser("gen", help="write and validate all generated configs")
    matrix(p)
    p.add_argument("--out", default=os.path.join(HERE, "work", "generated"))
    p = sub.add_parser("report", help="(re)build report.md for a results directory")
    p.add_argument("run_dir")
    p = sub.add_parser("restore", help="kill test apps, bind NICs back to the kernel driver")
    p.add_argument("--role", choices=("client", "server"), required=True)

    a = ap.parse_args(argv)
    util.VERBOSE = a.verbose
    if not a.cmd:
        ap.print_help()
        return 2
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        cfg = Config(a.config)
        return {"check": cmd_check, "build": cmd_build, "agent": cmd_agent, "run": cmd_run,
                "plan": cmd_plan, "gen": cmd_gen, "report": cmd_report,
                "restore": cmd_restore}[a.cmd](cfg, a)
    except BenchError as e:
        util.kill_all_procs()
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        util.kill_all_procs()
        print("interrupted", file=sys.stderr)
        return 130
