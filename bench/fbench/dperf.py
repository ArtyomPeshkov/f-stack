"""dperf: configuration generation, live statistics parsing, window summaries."""

import math
import os
import re
import time

from .util import Proc, die, run, strip_ansi, warn

# sizeof(struct socket) in dperf built with HTTP_PARSE
SOCKET_BYTES = 80
# 65536 mbufs per queue: ~2.4 KB per standard mbuf, ~11.5 KB with jumbo
MBUF_POOL_MB = 155
MBUF_POOL_JUMBO_MB = 750
DPDK_BASE_MB = 512
RTO_SEC = 2          # dperf default/minimum retransmit timeout
WAIT_SEC = 3         # dperf client 'wait' before the first launch
DRAIN_SEC = 4        # dperf prints 4 more seconds after 'duration'
LPORTS = 65535

VXLAN_HDR = 50
MTU_STD = 1500
JUMBO_MTU = 9000


def bond_spec(pcis, mode, policy):
    pol = {"l2": 0, "l23": 1, "l34": 2}[policy]
    return "bond%d:%d(%s)" % (mode, pol, ",".join(pcis))


class Port(object):
    """One dperf 'port' line plus its client/server/vxlan lines."""

    def __init__(self, dev, local_ip, gw_ip, client_base, client_num, server_base, server_num,
                 vxlan=None):
        self.dev = dev
        self.local_ip = local_ip
        self.gw_ip = gw_ip
        self.client_base = client_base
        self.client_num = client_num
        self.server_base = server_base
        self.server_num = server_num
        self.vxlan = vxlan  # dict(vni, smac, dmac, local, local_num, remote, remote_num)


def gen_conf(mode, cpus, ports, opts, comment=None):
    """opts: list of (keyword, value-or-None) in output order."""
    lines = []
    if comment:
        for c in comment.splitlines():
            lines.append("# " + c)
    lines.append("%-16s %s" % ("mode", mode))
    lines.append("%-16s %s" % ("cpu", " ".join(str(c) for c in cpus)))
    for k, v in opts:
        if v is None or v is False:
            continue
        if v is True:
            lines.append(k)
        else:
            lines.append("%-16s %s" % (k, v))
    for p in ports:
        lines.append("%-16s %-40s %-15s %s" % ("port", p.dev, p.local_ip, p.gw_ip))
    for p in ports:
        lines.append("%-16s %-15s %d" % ("client", p.client_base, p.client_num))
    for p in ports:
        lines.append("%-16s %-15s %d" % ("server", p.server_base, p.server_num))
    for p in ports:
        if p.vxlan:
            v = p.vxlan
            lines.append("%-16s %d %s %s %s %d %s %d" % ("vxlan", v["vni"], v["smac"], v["dmac"],
                                                         v["local"], v["local_num"], v["remote"],
                                                         v["remote_num"]))
    lines.append("%-16s %d %d" % ("listen", 80, 1))
    return "\n".join(lines) + "\n"


def memory_mb(workers, sockets_per_worker, jumbo):
    pool = MBUF_POOL_JUMBO_MB if jumbo else MBUF_POOL_MB
    return DPDK_BASE_MB + workers * pool + workers * sockets_per_worker * SOCKET_BYTES / 1048576.0


def client_ips_for(cps_per_worker, ports_per_ip=LPORTS):
    """Number of client IPs so that each worker has enough sockets for
    cps * RTO (dperf refuses to start otherwise) with 20% headroom."""
    need = cps_per_worker * RTO_SEC * 1.2
    return max(1, int(math.ceil(need / float(ports_per_ip))))


def validate_conf(dperf_bin, path):
    rc, out = run([dperf_bin, "-t", "-c", path], check=False, timeout=30)
    if rc != 0 or "Config file OK" not in out:
        die("dperf rejected %s:\n%s" % (path, out.strip()[-1500:]))


# ------------------------------------------------------------------ parsing
NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")


class StatsParser(object):
    """Incremental parser of dperf's per-second statistics output.

    dperf prints one block per second starting with 'seconds N cpuUsage a b c'
    and ending with 'ierrors .. oerrors .. imissed ..'. After the run it prints
    'Total Numbers:' followed by one more block (totals)."""

    def __init__(self, on_block=None):
        self.on_block = on_block
        self.blocks = []
        self.totals = None
        self.cur = None
        self.in_totals = False
        self.errors = []
        self.port_error = None

    def feed(self, raw):
        line = strip_ansi(raw).strip()
        if not line:
            return
        if line.startswith("Total Numbers"):
            self.in_totals = True
            self.cur = {"sec": -1, "t": time.time()}
            return
        low = line.lower()
        if ("error" in low or "fail" in low) and not line.startswith(("ierrors", "skOpen", "httpGet",
                                                                     "tcpDrop", "udpRt", "synRt",
                                                                     "pktRx", "tosRx")):
            self.errors.append(line)
            if ("fdir" in low or "please use 'rss'" in low or "flow create error" in low
                    or "flow init fail" in low):
                self.port_error = "fdir"
        toks = line.split()
        if toks[0] == "seconds" and len(toks) >= 2 and toks[1].isdigit():
            self.cur = {"sec": int(toks[1]), "t": time.time()}
            self._kv(toks, self.cur)
            return
        if self.cur is None:
            return
        if toks[0] in ("pktRx", "tcpRx", "arpRx", "tosRx", "synRx", "synRt", "tcpDrop", "udpRt",
                       "skOpen", "httpGet", "tcpReq", "ierrors", "kniRx"):
            self._kv(toks, self.cur)
            if toks[0] == "ierrors":
                blk, self.cur = self.cur, None
                # dperf prints either the HTTP counters (payload >= 85 bytes) or
                # tcpReq/tcpRsp; 'rsp'/'req' are whichever of them is present
                if "http2XX" in blk:
                    blk["rsp"] = blk.get("http2XX", 0)
                    blk["req"] = blk.get("httpGet", 0) + blk.get("httpPost", 0)
                else:
                    blk["rsp"] = blk.get("tcpRsp", 0)
                    blk["req"] = blk.get("tcpReq", 0)
                if self.in_totals:
                    self.totals = blk
                else:
                    self.blocks.append(blk)
                    if self.on_block:
                        self.on_block(blk)

    @staticmethod
    def _num(s):
        s = s.replace(",", "")
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return None

    def _kv(self, toks, d):
        i = 0
        while i < len(toks):
            key = toks[i]
            if key == "cpuUsage":
                vals = []
                i += 1
                while i < len(toks) and toks[i].isdigit():
                    vals.append(int(toks[i]))
                    i += 1
                d["cpu"] = vals
                continue
            if i + 1 < len(toks) and NUM_RE.match(toks[i + 1]):
                v = self._num(toks[i + 1])
                if v is not None and key != "seconds":
                    d[key] = v
                i += 2
            else:
                i += 1


RATE_KEYS = ("rsp", "req", "pktRx", "pktTx", "bitsRx", "bitsTx", "dropTx", "tcpRx", "tcpTx", "udpRx", "udpTx",
             "arpRx", "arpTx", "synRx", "synTx", "finRx", "finTx", "rstRx", "rstTx", "synRt",
             "finRt", "ackRt", "pushRt", "tcpDrop", "udpDrop", "udpRt", "ackDup", "skOpen",
             "skClose", "skErr", "httpGet", "http2XX", "httpErr", "tcpReq", "tcpRsp", "otherRx",
             "badRx")
ERR_KEYS = ("skErr", "httpErr", "tcpDrop", "udpDrop", "dropTx", "badRx")
RETRANS_KEYS = ("synRt", "finRt", "ackRt", "pushRt", "udpRt")


def summarize(blocks, prev_block=None):
    """Average rates over a list of per-second blocks."""
    n = len(blocks)
    s = {"seconds": n}
    if not n:
        return s
    for k in RATE_KEYS:
        s[k] = sum(b.get(k, 0) for b in blocks) / float(n)
    s["skCon"] = blocks[-1].get("skCon", 0)
    cpus = [b.get("cpu") or [] for b in blocks]
    per_core = [sum(c) / float(len(c)) for c in cpus if c]
    s["cpu_avg"] = sum(per_core) / len(per_core) if per_core else 0.0
    if cpus and cpus[0]:
        width = max(len(c) for c in cpus)
        cols = [[c[i] for c in cpus if len(c) > i] for i in range(width)]
        s["cpu_max_worker"] = max(sum(col) / float(len(col)) for col in cols if col)
        s["workers"] = width
    else:
        s["cpu_max_worker"] = 0.0
    for k in ("ierrors", "oerrors", "imissed"):
        base = (prev_block or {}).get(k, blocks[0].get(k, 0))
        s[k] = max(0, blocks[-1].get(k, 0) - base)
    s["errors"] = sum(s.get(k, 0) for k in ERR_KEYS) * n
    s["retrans"] = sum(s.get(k, 0) for k in RETRANS_KEYS) * n
    s["gbps_rx"] = s["bitsRx"] / 1e9
    s["gbps_tx"] = s["bitsTx"] / 1e9
    s["mpps_rx"] = s["pktRx"] / 1e6
    s["mpps_tx"] = s["pktTx"] / 1e6
    return s


def best_window(blocks, key, width=5):
    """Max average of `key` over `width` consecutive seconds -> (value, start_index)."""
    best, at = 0.0, 0
    for i in range(0, max(0, len(blocks) - width + 1)):
        v = sum(b.get(key, 0) for b in blocks[i:i + width]) / float(width)
        if v > best:
            best, at = v, i
    return best, at


class ClientRun(object):
    """Runs a dperf client and reports window boundaries via callbacks."""

    def __init__(self, dperf_bin, conf_path, log_path, slow_start, duration, settle,
                 on_window_start=None, on_window_stop=None, fail_fast_key=None):
        self.bin = dperf_bin
        self.conf = conf_path
        self.log = log_path
        self.S = slow_start
        self.D = duration
        self.settle = settle
        self.on_start = on_window_start
        self.on_stop = on_window_stop
        self.fail_fast_key = fail_fast_key
        self.parser = StatsParser(on_block=self._block)
        self.win_first = slow_start + settle
        self.win_last = slow_start + duration - 1
        self.t_window_start = None
        self.t_window_stop = None
        self.aborted = None
        self.proc = None

    def _block(self, b):
        sec = b["sec"]
        if sec == self.win_first - 1 and self.on_start:
            self.t_window_start = time.time()
            self.on_start()
        if sec == self.win_last and self.on_stop:
            self.t_window_stop = time.time()
            self.on_stop()
        # fail fast: nothing comes back from the server after the ramp started
        if self.fail_fast_key and sec == WAIT_SEC + 7:
            got = sum(x.get(self.fail_fast_key, 0) for x in self.parser.blocks)
            if got == 0 and self.proc:
                self.aborted = "no %s received during the first %d seconds" % (self.fail_fast_key, sec)
                self.proc.send_signal(2)

    def run(self):
        total = WAIT_SEC + self.S + self.D + DRAIN_SEC + 60
        self.proc = Proc([self.bin, "-c", self.conf], self.log, on_line=self.parser.feed)
        rc = self.proc.wait(timeout=total)
        if rc is None:
            warn("dperf client did not finish in %ds, stopping it" % total)
            self.proc.stop()
            rc = self.proc.p.returncode
        self.rc = rc
        return rc

    def window_blocks(self):
        return [b for b in self.parser.blocks if self.win_first <= b["sec"] <= self.win_last]

    def prev_block(self):
        prev = [b for b in self.parser.blocks if b["sec"] == self.win_first - 1]
        return prev[0] if prev else None


class ServerRun(object):
    """dperf server process managed by the agent."""

    def __init__(self, dperf_bin, conf_path, log_path):
        self.parser = StatsParser()
        self.allocated = 0
        self.proc = Proc([dperf_bin, "-c", conf_path], log_path, on_line=self._line)

    def _line(self, line):
        if line.startswith("socket allocation succeeded"):
            self.allocated += 1
        self.parser.feed(line)

    def wait_ready(self, timeout, workers):
        """Ready = every worker allocated its sockets. dperf then waits (up to
        60 s, answering ARP) for the gateway MAC, i.e. for the client to come up;
        the per-second statistics start only after that."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.parser.blocks or self.allocated >= workers:
                time.sleep(0.5)
                return self.proc.alive()
            if not self.proc.alive():
                return False
            time.sleep(0.3)
        return False

    def blocks_between(self, t0, t1):
        # a block printed at time t covers the second (t-1, t]
        return [b for b in self.parser.blocks if t0 + 0.5 <= b["t"] <= t1 + 0.5]

    def stop(self):
        self.proc.stop(timeout=20)
        return self.parser.totals


def conf_path_for(work, name):
    return os.path.join(work, name + ".conf")
