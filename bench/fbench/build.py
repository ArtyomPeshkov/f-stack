"""Builds everything against the DPDK that lives inside the F-Stack tree:
dperf, libfstack, F-Stack tools, nginx (F-Stack and kernel variants of the
same source tree app/nginx-*)."""

import glob
import os
import shutil

from .util import die, info, run, warn, which

PC_SUBDIRS = ("lib/x86_64-linux-gnu/pkgconfig", "lib64/pkgconfig", "lib/pkgconfig",
              "lib/aarch64-linux-gnu/pkgconfig", "share/pkgconfig")


def find_dpdk_pkgconfig(dpdk_build):
    """Directory to put into PKG_CONFIG_PATH so that `pkg-config libdpdk` resolves
    to the DPDK in `dpdk_build` (a meson build dir or an install prefix)."""
    if not os.path.isdir(dpdk_build):
        die("DPDK build directory not found: %s\n"
            "Build it first, e.g.: cd <f-stack>/dpdk && meson setup ../build/dpdk-build && "
            "ninja -C ../build/dpdk-build" % dpdk_build)
    unin = os.path.join(dpdk_build, "meson-uninstalled")
    if os.path.isfile(os.path.join(unin, "libdpdk-uninstalled.pc")):
        if not glob.glob(os.path.join(dpdk_build, "lib", "librte_eal.a")):
            die("%s is a meson build dir but librte_eal.a is missing: run `ninja -C %s`" %
                (dpdk_build, dpdk_build))
        return unin, "build-dir"
    for sub in PC_SUBDIRS:
        d = os.path.join(dpdk_build, sub)
        if os.path.isfile(os.path.join(d, "libdpdk.pc")):
            return d, "prefix"
    hits = [p for p in glob.glob(os.path.join(dpdk_build, "**", "libdpdk.pc"), recursive=True)
            if "meson-private" not in p]
    if hits:
        return os.path.dirname(hits[0]), "prefix"
    die("no libdpdk.pc / meson-uninstalled/libdpdk-uninstalled.pc under %s" % dpdk_build)


def build_env(cfg):
    pc_dir, kind = find_dpdk_pkgconfig(cfg.dpdk_build)
    env = dict(os.environ)
    env["PKG_CONFIG_PATH"] = pc_dir + (":" + env["PKG_CONFIG_PATH"] if env.get("PKG_CONFIG_PATH")
                                       else "")
    env["FF_PATH"] = cfg.fstack_dir
    env["FF_DPDK"] = cfg.dpdk_build
    env.pop("RTE_SDK", None)  # dperf would switch to the legacy make system
    rc, ver = run(["pkg-config", "--modversion", "libdpdk"], env=env, check=False)
    if rc != 0:
        die("pkg-config cannot resolve libdpdk from %s:\n%s" % (pc_dir, ver))
    info("DPDK %s from %s (%s)" % (ver.strip(), cfg.dpdk_build, kind))
    return env


def jobs():
    return str(max(1, os.cpu_count() or 1))


def newest_mtime(paths):
    m = 0.0
    for p in paths:
        try:
            m = max(m, os.path.getmtime(p))
        except OSError:
            pass
    return m


def dpdk_libs_mtime(cfg):
    """Newest DPDK static library: binaries linked before it are stale (DPDK
    primary and secondary processes, e.g. F-Stack workers and ff_* tools,
    must come from the same DPDK build)."""
    files = []
    # meson build dir: lib/, drivers/; install prefix: lib/, lib64/, lib/<triplet>/
    for pat in ("lib*/librte_*.a", "lib/*/librte_*.a", "drivers/librte_*.a"):
        files += glob.glob(os.path.join(cfg.dpdk_build, pat))
    return newest_mtime(files)


def older_than(path, mtime):
    try:
        return os.path.getmtime(path) < mtime
    except OSError:
        return True


def update_dpdk(cfg):
    """Incremental `ninja` in a meson build dir, so that DPDK source changes
    (e.g. the bonding fix in dpdk/drivers/net/bonding) reach every binary."""
    if not os.path.isfile(os.path.join(cfg.dpdk_build, "build.ninja")):
        return
    if not which("ninja"):
        warn("ninja not found: cannot check that %s is up to date" % cfg.dpdk_build)
        return
    rc, out = run(["ninja", "-C", cfg.dpdk_build], check=False, timeout=7200)
    if rc != 0:
        die("ninja -C %s failed:\n%s" % (cfg.dpdk_build, out[-3000:]))
    if "no work to do" not in out:
        info("DPDK rebuilt in %s" % cfg.dpdk_build)


def fstack_lib_sources_mtime(cfg):
    lib = os.path.join(cfg.fstack_dir, "lib")
    return newest_mtime(glob.glob(os.path.join(lib, "*.[ch]")) +
                        [os.path.join(lib, "Makefile"), os.path.join(lib, "ff_api.symlist")])


DPERF_CSUM_BUG = "csum_tcp = csum_update_u32(csum_tcp, htonl(sk->snd_nxt), htonl(sk->rcv_nxt));"
DPERF_CSUM_FIX = "csum_tcp = csum_update_u32(csum_tcp, htonl(sk->snd_una), htonl(sk->rcv_nxt));"


def fix_dperf_vxlan_csum(dperf_dir):
    """dperf writes th_seq = snd_una but updates the inner (VXLAN) TCP checksum
    with snd_nxt: SYN/FIN/data segments get a wrong checksum and are dropped by
    Linux / F-Stack (see patches/dperf-vxlan-inner-tcp-csum.patch)."""
    path = os.path.join(dperf_dir, "src", "tcp.c")
    try:
        with open(path) as f:
            src = f.read()
    except (IOError, OSError):
        return False
    if DPERF_CSUM_BUG not in src:
        return False
    with open(path, "w") as f:
        f.write(src.replace(DPERF_CSUM_BUG, DPERF_CSUM_FIX))
    warn("patched %s: VXLAN inner TCP checksum (snd_nxt -> snd_una), needed for scenario 2 "
         "with VXLAN; see bench/patches/dperf-vxlan-inner-tcp-csum.patch" % path)
    return True


def build_dperf(cfg, env, force=False):
    if not os.path.isfile(os.path.join(cfg.dperf_dir, "Makefile")):
        die("dperf sources not found in %s (set [paths] dperf_dir)" % cfg.dperf_dir)
    out = os.path.join(cfg.dperf_dir, "build", "dperf")
    if fix_dperf_vxlan_csum(cfg.dperf_dir):
        force = True
    if not force and older_than(out, dpdk_libs_mtime(cfg)):
        if os.path.isfile(out):
            info("dperf is older than the DPDK libraries, rebuilding")
        force = True
    if force:
        info("building dperf in %s" % cfg.dperf_dir)
        run(["make", "-C", cfg.dperf_dir, "clean"], env=env, check=False, timeout=120)
        run(["make", "-C", cfg.dperf_dir, "-j", jobs()], env=env, timeout=1800)
    rc, ver = run([out, "-v"], check=False)
    info("dperf %s: %s" % (ver.strip(), out))
    try:
        major, minor = [int(x) for x in ver.strip().split(".")[:2]]
        if (major, minor) < (1, 8):
            warn("dperf %s is old; the generated configs target dperf >= 1.8" % ver.strip())
    except ValueError:
        pass
    return out


def build_fstack_lib(cfg, env, force=False):
    lib = os.path.join(cfg.fstack_dir, "lib")
    # `make` re-archives libfstack.a on every run, so it is run only when the
    # library is missing or older than its sources or than DPDK
    if force or older_than(os.path.join(lib, "libfstack.a"),
                           max(fstack_lib_sources_mtime(cfg), dpdk_libs_mtime(cfg))):
        info("building libfstack")
        if force:
            run(["make", "-C", lib, "clean"], env=env, check=False, timeout=300)
        run(["make", "-C", lib, "-j", jobs()], env=env, timeout=3600)
    return os.path.join(lib, "libfstack.a")


TOOLS = ("ifconfig", "route", "arp", "top", "netstat")


def build_tools(cfg, env, force=False):
    sbin = os.path.join(cfg.fstack_dir, "tools", "sbin")
    tools = os.path.join(cfg.fstack_dir, "tools")
    stale = any(older_than(os.path.join(sbin, t), dpdk_libs_mtime(cfg)) for t in TOOLS)
    if force or stale:
        info("building F-Stack tools (ff_ifconfig, ff_route, ff_arp, ff_top, ...)")
        # the tools are DPDK secondary processes of the F-Stack application:
        # relink them from scratch against the current DPDK
        run(["make", "-C", tools, "clean"], env=env, check=False, timeout=600)
        run(["make", "-C", tools], env=env, timeout=3600)
    return sbin


def nginx_src(cfg):
    cands = sorted(glob.glob(os.path.join(cfg.fstack_dir, "app", "nginx-*")))
    cands = [c for c in cands if os.path.isfile(os.path.join(c, "configure"))]
    if not cands:
        die("no app/nginx-* in %s" % cfg.fstack_dir)
    return cands[-1]


def build_nginx(cfg, env, variant, force=False):
    """variant: 'fstack' (--with-ff_module) or 'kernel' (same source, plain sockets)."""
    prefix = os.path.join(cfg.install_dir, "nginx-%s" % variant)
    binary = os.path.join(prefix, "sbin", "nginx")
    # both variants link libfstack and DPDK
    deps = max(dpdk_libs_mtime(cfg),
               newest_mtime([os.path.join(cfg.fstack_dir, "lib", "libfstack.a")]))
    if os.path.isfile(binary) and not force:
        if not older_than(binary, deps):
            info("nginx-%s is up to date: %s" % (variant, binary))
            return binary
        info("nginx-%s is older than libfstack/DPDK, rebuilding" % variant)
    src = nginx_src(cfg)
    bdir = os.path.join(cfg.work_dir, "build", "nginx-src-%s" % variant)
    if os.path.isdir(bdir):
        shutil.rmtree(bdir)
    shutil.copytree(src, bdir, symlinks=True)
    args = ["./configure", "--prefix=%s" % prefix, "--with-http_stub_status_module"]
    if variant == "fstack":
        args.append("--with-ff_module")
    info("building nginx-%s from %s" % (variant, src))
    run(args, cwd=bdir, env=env, timeout=600)
    rc, out = run(["make", "-j", jobs()], cwd=bdir, env=env, check=False, timeout=3600)
    if rc != 0 and "ff_mbuf_set_timestamp" in out:
        warn("libfstack.a was built before ff_mbuf_set_timestamp was exported; rebuilding it")
        build_fstack_lib(cfg, env, force=True)
        rc, out = run(["make", "-j", jobs()], cwd=bdir, env=env, check=False, timeout=3600)
    if rc != 0:
        die("nginx-%s build failed:\n%s" % (variant, out[-3000:]))
    run(["make", "install"], cwd=bdir, env=env, timeout=600)
    info("nginx-%s: %s" % (variant, binary))
    return binary


def stale_binaries(cfg):
    """Built binaries older than the DPDK libraries (or libfstack) they link."""
    dpdk = dpdk_libs_mtime(cfg)
    if not dpdk:
        return []
    out = []
    bins = [cfg.dperf_bin] + [os.path.join(cfg.fstack_tools, t) for t in TOOLS]
    for p in bins:
        if os.path.isfile(p) and older_than(p, dpdk):
            out.append(p)
    fst = max(dpdk, newest_mtime([os.path.join(cfg.fstack_dir, "lib", "libfstack.a")]))
    for p in (cfg.nginx_fstack, cfg.nginx_kernel):
        if p and os.path.isfile(p) and older_than(p, fst):
            out.append(p)
    return out


def build_all(cfg, what, force=False):
    update_dpdk(cfg)
    env = build_env(cfg)
    what = set(what)
    if "all" in what:
        what = {"dperf", "fstack", "tools", "nginx"}
    if "dperf" in what:
        build_dperf(cfg, env, force)
    if what & {"fstack", "tools", "nginx"}:
        build_fstack_lib(cfg, env, force and "fstack" in what)
    if "tools" in what:
        build_tools(cfg, env, force)
    if "nginx" in what:
        if cfg.raw["paths"]["nginx_fstack"]:
            info("using prebuilt F-Stack nginx %s" % cfg.nginx_fstack)
        else:
            build_nginx(cfg, env, "fstack", force)
        if cfg.raw["paths"]["nginx_kernel"]:
            info("using prebuilt kernel nginx %s" % cfg.nginx_kernel)
        else:
            build_nginx(cfg, env, "kernel", force)
