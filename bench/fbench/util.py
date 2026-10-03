"""Small helpers shared by all fbench modules (python >= 3.6, stdlib only)."""

import datetime
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

VERBOSE = False
_log_lock = threading.Lock()
_log_file = None


class BenchError(Exception):
    """Expected failure with a human readable message."""


def set_log_file(path):
    global _log_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _log_file = open(path, "a", buffering=1)


def _emit(prefix, msg, to_console=True):
    line = "%s %s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), prefix, msg)
    with _log_lock:
        if to_console:
            print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")


def info(msg):
    _emit("[*]", msg)


def warn(msg):
    _emit("[!]", msg)


def debug(msg):
    _emit("[.]", msg, to_console=VERBOSE)


def die(msg):
    raise BenchError(msg)


def now_ts():
    return time.time()


def run(cmd, check=True, timeout=120, env=None, cwd=None, quiet=False, input_text=None):
    """Run a command (list or shell string). Returns (rc, stdout+stderr)."""
    shell = isinstance(cmd, str)
    shown = cmd if shell else " ".join(shlex.quote(c) for c in cmd)
    debug("run: %s" % shown)
    try:
        p = subprocess.run(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, errors="replace", timeout=timeout, env=env,
                           cwd=cwd, input=input_text)
    except subprocess.TimeoutExpired:
        if check:
            die("command timed out after %ss: %s" % (timeout, shown))
        return 124, "timeout"
    except FileNotFoundError as e:
        if check:
            die("command not found: %s (%s)" % (shown, e))
        return 127, str(e)
    out = p.stdout or ""
    if p.returncode != 0:
        if check:
            die("command failed (rc=%d): %s\n%s" % (p.returncode, shown, out.strip()[-2000:]))
        if not quiet:
            debug("rc=%d: %s -> %s" % (p.returncode, shown, out.strip()[-500:]))
    return p.returncode, out


def which(name):
    for d in os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin").split(":") + ["/usr/sbin", "/sbin"]:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def read_file(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except (IOError, OSError):
        return default


def write_file(path, data, mode="w"):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, mode) as f:
        f.write(data)


def write_sys(path, value, check=False):
    """Write to a sysfs/procfs file; returns True on success."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except (IOError, OSError) as e:
        if check:
            die("cannot write %r to %s: %s" % (value, path, e))
        debug("cannot write %r to %s: %s" % (value, path, e))
        return False


def parse_cpu_list(text):
    """'2-5,8 10' -> [2, 3, 4, 5, 8, 10] (order preserved, no duplicates)."""
    cpus = []
    if text is None:
        return cpus
    for part in re.split(r"[,\s]+", str(text).strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = [int(part)]
        for c in rng:
            if c not in cpus:
                cpus.append(c)
    return cpus


def cpu_list_str(cpus):
    """[2,3,4,7] -> '2-4,7'"""
    cpus = sorted(set(cpus))
    out = []
    i = 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else "%d-%d" % (cpus[i], cpus[j]))
        i = j + 1
    return ",".join(out)


def cpu_mask_hex(cpus):
    mask = 0
    for c in cpus:
        mask |= 1 << c
    return "%x" % mask


def parse_size(text):
    """'1m' -> 1048576, '64k' -> 65536, '600' -> 600 (binary units for sizes)."""
    s = str(text).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)([kmg]?)b?$", s)
    if not m:
        die("bad size: %r" % text)
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]
    return int(float(m.group(1)) * mult)


def parse_rate(text):
    """'2m' -> 2000000, '300k' -> 300000 (decimal units for rates)."""
    s = str(text).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)([kmg]?)$", s)
    if not m:
        die("bad rate: %r" % text)
    mult = {"": 1, "k": 1000, "m": 1000 ** 2, "g": 1000 ** 3}[m.group(2)]
    return int(float(m.group(1)) * mult)


def size_label(nbytes):
    for unit, div in (("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024)):
        if nbytes >= div and nbytes % div == 0:
            return "%d%s" % (nbytes // div, unit)
    return str(nbytes)


def split_words(text):
    return [w for w in re.split(r"[,\s]+", (text or "").strip()) if w]


def human(n, unit=""):
    n = float(n)
    for suffix, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return "%.2f%s%s" % (n / div, suffix, unit)
    return "%.0f%s" % (n, unit)


_live_procs = set()


def kill_all_procs():
    """Stop every background process still alive (used on Ctrl-C / errors)."""
    for pr in list(_live_procs):
        try:
            pr.stop(timeout=10)
        except Exception:
            pass


class Proc(object):
    """Background process with its output appended to a log file and fed
    line-by-line to an optional callback (called from a reader thread)."""

    def __init__(self, cmd, log_path, on_line=None, env=None, cwd=None):
        self.cmd = cmd
        self.log_path = log_path
        self.on_line = on_line
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._log = open(log_path, "a", buffering=1)
        self._log.write("# %s %s\n" % (datetime.datetime.now().isoformat(),
                                         " ".join(shlex.quote(c) for c in cmd)))
        debug("spawn: %s (log %s)" % (" ".join(cmd), log_path))
        # dperf/ff_top use stdio: through a pipe their output would come in 4 KB
        # chunks, but the per-second statistics are needed in real time
        self._pty = None
        if which("stdbuf"):
            out = subprocess.PIPE
            argv = ["stdbuf", "-oL", "-eL"] + list(cmd)
        else:
            import pty
            self._pty, out = pty.openpty()
            argv = list(cmd)
        self.p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                  start_new_session=True)
        if self._pty is not None:
            os.close(out)
            self._stream = os.fdopen(self._pty, "rb", buffering=0)
        else:
            self._stream = self.p.stdout
        self.lines = []
        _live_procs.add(self)
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()

    def _reader(self):
        buf = b""
        while True:
            try:
                chunk = self._stream.read1(65536) if hasattr(self._stream, "read1") \
                    else self._stream.read(65536)
            except (OSError, ValueError):  # EIO on the pty when the child is gone
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._line(raw.decode("utf-8", "replace").rstrip("\r"))
        if buf:
            self._line(buf.decode("utf-8", "replace").rstrip("\r"))

    def _line(self, line):
        self._log.write(line + "\n")
        self.lines.append(line)
        if len(self.lines) > 20000:
            del self.lines[:5000]
        if self.on_line:
            try:
                self.on_line(line)
            except Exception as e:  # never kill the reader
                debug("on_line callback error: %s" % e)

    @property
    def pid(self):
        return self.p.pid

    def alive(self):
        return self.p.poll() is None

    def wait(self, timeout=None):
        try:
            rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._t.join(5)
        _live_procs.discard(self)
        return rc

    def send_signal(self, sig):
        try:
            os.killpg(self.p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, sig=signal.SIGINT, timeout=20):
        """Ask the process to stop, escalate to SIGKILL after timeout."""
        if self.alive():
            self.send_signal(sig)
            if self.wait(timeout) is None:
                warn("pid %d did not stop after %s, killing" % (self.p.pid, sig))
                self.send_signal(signal.SIGKILL)
                self.wait(10)
        self._t.join(5)
        _live_procs.discard(self)
        try:
            self._log.close()
        except Exception:
            pass
        return self.p.returncode

    def tail(self, n=40):
        return "\n".join(self.lines[-n:])


def pids_of(exe_path):
    """PIDs whose /proc/<pid>/exe resolves to exe_path."""
    real = os.path.realpath(exe_path)
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            exe = os.readlink("/proc/%s/exe" % d)
        except OSError:
            continue
        if exe.endswith(" (deleted)"):
            exe = exe[:-10]
        if exe == real:
            out.append(int(d))
    return out


def kill_exe(exe_path, timeout=15):
    """Terminate all processes of a binary: SIGTERM, then SIGKILL."""
    pids = pids_of(exe_path)
    if not pids:
        return
    debug("terminating %s: %s" % (exe_path, pids))
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
        for pid in pids_of(exe_path):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.time() + wait
        while time.time() < deadline and pids_of(exe_path):
            time.sleep(0.3)
        if not pids_of(exe_path):
            return
    if pids_of(exe_path):
        warn("processes of %s still alive: %s" % (exe_path, pids_of(exe_path)))


def wait_until(pred, timeout, interval=0.5, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    debug("timeout waiting for %s" % what)
    return False


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def require_root():
    if os.geteuid() != 0:
        die("must be run as root (DPDK, NIC binding, sysctl, hugepages)")


def py_version_ok():
    return sys.version_info >= (3, 6)
