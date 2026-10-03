"""Server-side agent and its client proxy.

The orchestrator (client machine) drives the server machine through a small
JSON-lines protocol over TCP on the management network. Only a fixed set of
commands is accepted, every request must carry the shared token."""

import json
import os
import shlex
import socket
import subprocess
import threading
import time
import traceback

from . import VERSION
from . import build as buildmod
from . import cpu as cpumod
from . import dperf as dp
from . import fstack as fs
from . import host as hostmod
from . import kernelnet
from . import nginx as ngx
from . import nic as nicmod
from . import scenarios as sc
from .config import IpPlan
from .util import BenchError, die, info, kill_exe, pids_of, run, set_log_file, wait_until, warn, \
    write_file


class Agent(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.m = cfg.server
        self.plan = IpPlan(cfg)
        self.root = os.path.join(cfg.work_dir, "agent")
        os.makedirs(self.root, exist_ok=True)
        set_log_file(os.path.join(self.root, "agent.log"))
        self.nic = nicmod.NicManager(self.m, cfg.fstack_dir, cfg.dpdk_build)
        self.power = hostmod.PowerSettings()
        self.irqb = hostmod.IrqBalance()
        self.sysctl = hostmod.Sysctl()
        self.tools = fs.Tools(cfg.fstack_tools)
        self.mode = None            # "dpdk" | "kernel"
        self.dsrv = None            # dperf server (scenario 1)
        self.nginx = None
        self.stack = None
        self.knet = None
        self.knet_key = None
        self.sampler = None
        self.top = None
        self.win_t0 = None
        self.prepared = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def _rundir(self, name):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _stop_apps(self):
        if self.dsrv:
            self.dsrv.stop()
            self.dsrv = None
        if self.nginx:
            self.nginx.stop()
            self.nginx = None
        for b in (self.cfg.dperf_bin, self.cfg.nginx_fstack, self.cfg.nginx_kernel):
            kill_exe(b)

    def _to_dpdk(self, ports):
        if self.knet:
            self.knet.teardown()
            self.knet, self.knet_key = None, None
        if self.mode == "kernel":
            self.sysctl.restore()
        self.nic.bind_dpdk(ports)
        self.mode = "dpdk"

    def _to_kernel(self):
        ifnames = self.nic.bind_kernel()
        if self.mode != "kernel":
            for k, v in hostmod.KERNEL_SYSCTLS:
                self.sysctl.set(k, v)
            self.irqb.stop()
        self.mode = "kernel"
        return ifnames

    # ----------------------------------------------------------- commands
    def cmd_hello(self, digest, version):
        same = digest == self.cfg.digest()
        return {"version": VERSION, "hostname": socket.gethostname(), "digest_ok": same,
                "digest": self.cfg.digest(), "client_version_ok": version == VERSION,
                "stale": buildmod.stale_binaries(self.cfg)}

    def cmd_prepare(self):
        node = nicmod.numa_node(self.m.pci[0])
        hp = hostmod.ensure_hugepages(self.m.hugepages_gb, node)
        if not self.prepared:
            self.power.apply()
            self.prepared = True
        hi = hostmod.host_info(self.m)
        hi["hugepages"] = hp
        return hi

    def cmd_dperf_start(self, point, flow):
        self._stop_apps()
        nports = 2 if point["topology"] in ("bond", "dual") else 1
        self._to_dpdk(self.m.pci[:nports])
        d = self._rundir("s1")
        text = sc.s1_dperf_conf(self.cfg, "server", point, flow, self.plan)
        path = os.path.join(d, "dperf-server.conf")
        write_file(path, text)
        dp.validate_conf(self.cfg.dperf_bin, path)
        log = os.path.join(d, "dperf-server.log")
        if os.path.exists(log):
            os.remove(log)
        self.dsrv = dp.ServerRun(self.cfg.dperf_bin, path, log)
        timeout = 90 if point["topology"] == "bond" else 60
        if not self.dsrv.wait_ready(timeout, point["n"]):
            tail = self.dsrv.proc.tail(30)
            fdir = self.dsrv.parser.port_error == "fdir"
            self.dsrv.stop()
            self.dsrv = None
            raise BenchError("%sdperf server did not start:\n%s" % ("FDIR-UNSUPPORTED " if fdir else "",
                                                                   tail))
        return {"conf": text}

    def cmd_dperf_stop(self):
        if not self.dsrv:
            return {}
        totals = self.dsrv.stop()
        log_tail = self.dsrv.proc.tail(40)
        self.dsrv = None
        return {"totals": totals, "log_tail": log_tail}

    def cmd_window_start(self, cores, fstack_procs=0):
        self.win_t0 = time.time()
        self.sampler = cpumod.Sampler(cores)
        self.top = None
        if fstack_procs:
            try:
                self.top = fs.TopSampler(self.tools, fstack_procs,
                                         os.path.join(self._rundir("s2"), "ff_top.log"))
            except BenchError as e:
                warn("ff_top unavailable: %s" % e)
        return {"t0": self.win_t0}

    def cmd_window_stop(self):
        t1 = time.time()
        res = {"t0": self.win_t0, "t1": t1}
        if self.sampler:
            res["cpu"] = self.sampler.stop()
            self.sampler = None
        if self.top:
            res["fstack_top"] = self.top.stop()
            self.top = None
        if self.dsrv and self.win_t0:
            blocks = self.dsrv.blocks_between(self.win_t0, t1)
            res["dperf_server"] = dp.summarize(blocks)
            res["dperf_server_series"] = [{"t": b["t"] - self.win_t0, "pktRx": b.get("pktRx", 0),
                                           "cpu": b.get("cpu", [])} for b in blocks]
        return res

    def cmd_s2_start(self, point):
        stack, topo, vx, w = point["stack"], point["topology"], point["vxlan"], point["workers"]
        nports = 2 if topo in ("bond", "dual") else 1
        if w > len(self.m.cores):
            die("not enough server cores for %d workers" % w)
        cores = self.m.cores[:w]
        d = self._rundir("s2")
        if self.nginx:
            self.nginx.stop()
            self.nginx = None
        ngx.make_docroot(self.cfg.s2["bw_size"])
        out = {}
        if stack == "kernel":
            if self.mode != "kernel":
                self._stop_apps()
            ifnames = self._to_kernel()
            key = (topo, vx)
            if self.knet_key != key:
                if self.knet:
                    self.knet.teardown()
                self.knet = kernelnet.KernelNet(self.plan, ifnames, self.cfg.bond_mode,
                                                self.cfg.bond_xmit_policy)
                self.knet.setup(topo, vx, nports)
                self.knet_key = key
            tuning = []
            for i in range(nports):
                tuning.append(nicmod.tune_kernel_port(ifnames[i], self.m.pci[i], cores, vx))
            out["tuning"] = tuning
            out["links"] = [nicmod.link_info(ifnames[i]) for i in range(nports)]
            conf = ngx.gen_conf("kernel", d, w, cores, self.cfg.s2["small_body"], self.cfg.s2)
            self.nginx = ngx.Nginx(self.cfg.nginx_kernel, d, conf)
            self.nginx.test()
            self.nginx.start()
            self.nginx.wait_workers(w, 30)
            code = None
            for _ in range(20):
                code = ngx.http_probe("127.0.0.1", ngx.SMALL_PATH)
                if code == 200:
                    break
                time.sleep(0.5)
            if code != 200:
                die("kernel nginx does not answer on 127.0.0.1:80%s (got %s)\n%s" %
                    (ngx.SMALL_PATH, code, self.nginx.error_log_tail()))
            out["nginx_conf"] = conf
            if vx:
                out["inner_macs"] = [self.plan.server_inner_mac] * 2
        else:
            self._stop_apps()
            pcis = self.m.pci[:nports]
            self._to_dpdk(pcis)
            vips = 0 if vx else point["server_ips_per_port"]
            fconf = fs.gen_conf(cores, pcis, topo, self.plan, self.cfg.s2, self.cfg.bond_mode,
                                self.cfg.bond_xmit_policy, vips)
            fpath = os.path.join(d, "f-stack.conf")
            write_file(fpath, fconf)
            conf = ngx.gen_conf("fstack", d, w, cores, self.cfg.s2["small_body"], self.cfg.s2,
                                fstack_conf=fpath)
            self.nginx = ngx.Nginx(self.cfg.nginx_fstack, d, conf)
            self.nginx.test()
            for attempt in (1, 2):
                time.sleep(3)  # let the previous DPDK primary release its resources
                self.nginx.start()
                if self._fstack_up(w):
                    break
                tail = self.nginx.error_log_tail(40)
                self.nginx.stop()
                if attempt == 2:
                    die("F-Stack nginx did not come up\n%s" % tail)
                warn("F-Stack nginx did not come up, retrying once")
            if vx:
                nvx = 2 if topo == "dual" else 1
                vteps = [self.plan.vtep_base("client", i) for i in range(nvx)]
                out["inner_macs"] = fs.setup_vxlan(self.tools, w, topo, self.plan, nvx, vteps)
            out["nginx_conf"] = conf
            out["fstack_conf"] = fconf
        self.stack = stack
        return out

    def _fstack_up(self, w):
        """All F-Stack processes answer the tools (each runs its own stack)."""
        deadline = time.time() + 120
        if not wait_until(lambda: len(pids_of(self.nginx.bin)) >= w + 1 or
                          not self.nginx.check_alive(), 60, what="nginx workers"):
            return False
        while not self.tools.alive(w - 1):
            if time.time() > deadline or len(pids_of(self.nginx.bin)) < w + 1:
                return False
            time.sleep(2)
        return True

    def cmd_s2_stop(self):
        if self.nginx:
            tail = self.nginx.error_log_tail(15)
            self.nginx.stop()
            self.nginx = None
            return {"error_log_tail": tail}
        return {}

    def cmd_diag(self):
        out = {}
        if self.mode == "kernel":
            out["ip_link"] = run(["ip", "-s", "link"], check=False)[1][-4000:]
            out["ss"] = run(["ss", "-s"], check=False)[1]
            out["netstat"] = run("nstat -az 2>/dev/null | egrep -i 'ListenOverflows|ListenDrops|"
                                 "TCPBacklogDrop|SyncookiesSent|TCPAbortOnMemory|TCPRcvQDrop'",
                                 check=False)[1]
        if self.nginx:
            out["nginx_error_log"] = self.nginx.error_log_tail(30)
        out["dmesg"] = run("dmesg | tail -n 20", check=False)[1]
        return out

    def cmd_cleanup(self, restore_bindings=True):
        self._stop_apps()
        if self.knet:
            self.knet.teardown()
            self.knet, self.knet_key = None, None
        self.sysctl.restore()
        self.irqb.restore()
        if restore_bindings:
            try:
                self.nic.bind_kernel()
            except BenchError as e:
                warn("rebinding to kernel driver failed: %s" % e)
        self.mode = None
        return {}

    def shutdown(self):
        try:
            self.cmd_cleanup(restore_bindings=self.cfg.restore_bindings)
        finally:
            self.power.restore()

    # ------------------------------------------------------------ server
    COMMANDS = ("hello", "prepare", "dperf_start", "dperf_stop", "window_start", "window_stop",
                "s2_start", "s2_stop", "diag", "cleanup", "ping")

    def cmd_ping(self):
        return {"pong": time.time()}

    def handle(self, req):
        if req.get("token") != self.cfg.token:
            return {"ok": False, "error": "bad token"}
        cmd = req.get("cmd")
        if cmd == "shutdown":
            return {"ok": True, "result": {}, "shutdown": True}
        if cmd not in self.COMMANDS:
            return {"ok": False, "error": "unknown command %r" % cmd}
        try:
            with self.lock:
                res = getattr(self, "cmd_" + cmd)(**req.get("args", {}))
            return {"ok": True, "result": res}
        except BenchError as e:
            warn("%s failed: %s" % (cmd, e))
            return {"ok": False, "error": str(e)}
        except Exception as e:
            tb = traceback.format_exc()
            warn("%s crashed: %s" % (cmd, tb))
            return {"ok": False, "error": "%s: %s\n%s" % (type(e).__name__, e, tb[-1500:])}

    def serve(self, bind_addr, port):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind_addr, port))
        srv.listen(4)
        info("agent v%s listening on %s:%d (config digest %s)" % (VERSION, bind_addr or "*", port,
                                                                 self.cfg.digest()))
        stop = False
        try:
            while not stop:
                conn, peer = srv.accept()
                info("controller connected from %s:%d" % peer)
                f = conn.makefile("rwb", buffering=0)
                try:
                    for raw in f:
                        try:
                            req = json.loads(raw.decode())
                        except ValueError:
                            break
                        if req.get("cmd") not in ("ping", "window_start", "window_stop"):
                            info("<- %s" % req.get("cmd"))
                        resp = self.handle(req)
                        resp["id"] = req.get("id")
                        f.write((json.dumps(resp, default=str) + "\n").encode())
                        if resp.get("shutdown"):
                            stop = True
                            break
                except (OSError, ConnectionError) as e:
                    warn("connection error: %s" % e)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                info("controller disconnected")
                # a vanished controller must not leave load-dependent apps running
                with self.lock:
                    if self.sampler:
                        self.sampler.stop()
                        self.sampler = None
                    if self.top:
                        self.top.stop()
                        self.top = None
                    self._stop_apps()
        finally:
            srv.close()
            self.shutdown()
            info("agent stopped")


class AgentClient(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.sock = None
        self.f = None
        self.lock = threading.Lock()
        self.seq = 0
        self.ssh_started = False

    def _start_over_ssh(self):
        if not self.cfg.ssh:
            return False
        rdir = self.cfg.remote_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rconf = os.path.join(rdir, os.path.basename(self.cfg.path))
        remote = ("cd %s && nohup python3 fbench.py -c %s agent > %s/agent.out 2>&1 < /dev/null &"
                  % (shlex.quote(rdir), shlex.quote(rconf), shlex.quote(rdir)))
        info("starting the agent on %s over ssh" % self.cfg.ssh)
        rc, out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.cfg.ssh,
                       remote], check=False, timeout=60)
        if rc != 0:
            warn("ssh failed: %s" % out.strip())
            return False
        self.ssh_started = True
        return True

    def connect(self, wait=600):
        addr = (self.cfg.server_addr, self.cfg.port)
        tried_ssh = False
        deadline = time.time() + wait
        last_note = 0
        while True:
            try:
                s = socket.create_connection(addr, timeout=10)
                s.settimeout(None)
                self.sock = s
                self.f = s.makefile("rwb", buffering=0)
                return
            except OSError as e:
                if not tried_ssh:
                    tried_ssh = True
                    if self._start_over_ssh():
                        time.sleep(3)
                        continue
                if time.time() > deadline:
                    die("cannot connect to the agent at %s:%d: %s" % (addr[0], addr[1], e))
                if time.time() - last_note > 30:
                    info("waiting for the agent at %s:%d (start it on the server: "
                         "sudo ./agent.sh) ..." % addr)
                    last_note = time.time()
                time.sleep(2)

    def call(self, cmd, timeout=900, **args):
        with self.lock:
            self.seq += 1
            req = {"id": self.seq, "cmd": cmd, "token": self.cfg.token, "args": args}
            self.sock.settimeout(timeout)
            self.f.write((json.dumps(req) + "\n").encode())
            line = self.f.readline()
            if not line:
                die("agent closed the connection during %s" % cmd)
            resp = json.loads(line.decode())
            if not resp.get("ok"):
                raise BenchError("agent: %s: %s" % (cmd, resp.get("error")))
            return resp.get("result")

    def close(self, shutdown=False):
        if self.sock:
            try:
                if shutdown:
                    with self.lock:
                        self.f.write((json.dumps({"cmd": "shutdown", "token": self.cfg.token}) +
                                      "\n").encode())
                        self.f.readline()
                self.sock.close()
            except OSError:
                pass
            self.sock = None
