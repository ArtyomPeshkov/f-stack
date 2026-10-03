"""Builds everything against the DPDK that lives inside the F-Stack tree:
dperf, libfstack, F-Stack tools, nginx (F-Stack and kernel variants of the
same source tree app/nginx-*)."""

import glob
import os
import shutil

from .util import die, info, run, warn

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
    if force or not os.path.isfile(out):
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
    if force or not os.path.isfile(os.path.join(lib, "libfstack.a")):
        info("building libfstack")
        if force:
            run(["make", "-C", lib, "clean"], env=env, check=False, timeout=300)
        run(["make", "-C", lib, "-j", jobs()], env=env, timeout=3600)
    return os.path.join(lib, "libfstack.a")


def build_tools(cfg, env, force=False):
    sbin = os.path.join(cfg.fstack_dir, "tools", "sbin")
    need = ("ifconfig", "route", "arp", "top", "netstat")
    if force or not all(os.path.isfile(os.path.join(sbin, t)) for t in need):
        info("building F-Stack tools (ff_ifconfig, ff_route, ff_arp, ff_top, ...)")
        run(["make", "-C", os.path.join(cfg.fstack_dir, "tools")], env=env, timeout=3600)
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
    if os.path.isfile(binary) and not force:
        info("nginx-%s already built: %s" % (variant, binary))
        return binary
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


def build_all(cfg, what, force=False):
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
