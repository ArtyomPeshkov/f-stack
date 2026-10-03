"""Report generation from results.jsonl: markdown tables, CPU optimum, verdicts, CSV."""

import csv
import json
import os

from .util import size_label

TEST_TITLES = {
    1: {"tps": "TCP throughput, 1400B request/response, keep-alive (Gbps, RX+TX)",
        "pps": "UDP 64B packet rate, flood (Mpps received by the server)",
        "cps": "TCP connections per second, short connections",
        "bw": "TCP download, large responses (Gbps RX)"},
    2: {"rps": "HTTP keep-alive, small response (requests/s)",
        "cps": "HTTP short connections, small response (connections/s)",
        "bw": "HTTP keep-alive, large file download (Gbps)"},
}


def load(run_dir):
    recs = []
    path = os.path.join(run_dir, "results.jsonl")
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    # a re-run of the same point replaces the older record
    by_id = {}
    for r in recs:
        by_id[r["id"]] = r
    meta = {}
    mp = os.path.join(run_dir, "run.json")
    if os.path.isfile(mp):
        with open(mp) as f:
            meta = json.load(f)
    return list(by_id.values()), meta


def fmt(v, unit=None):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    if unit in ("Gbps", "Mpps", "cores"):
        return "%.2f" % v
    if unit == "%":
        return "%.0f" % v
    if abs(v) >= 1e6:
        return "%.3fM" % (v / 1e6)
    if abs(v) >= 1e3:
        return "%.1fk" % (v / 1e3)
    return "%.1f" % v


def optimum(points, tol):
    """points: [(n, value)] -> (n_opt, value_at_opt, max_value, n_max)"""
    pts = [(n, v) for n, v in points if v is not None]
    if not pts:
        return None, None, None, None
    n_max, vmax = max(pts, key=lambda x: (x[1], -x[0]))
    for n, v in sorted(pts):
        if v >= (1.0 - tol) * vmax:
            return n, v, vmax, n_max
    return n_max, vmax, vmax, n_max


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _ok(r):
    return r.get("status") == "ok" and r.get("metric")


def vx_label(v):
    return "VXLAN" if v else "no VXLAN"


def s1_section(recs, tol):
    L = ["## Scenario 1: dperf <-> dperf", ""]
    groups = {}
    for r in recs:
        if r.get("scenario") != 1:
            continue
        key = (r["topology"], r["vxlan"], bool(r.get("jumbo")), r["test"])
        groups.setdefault(key, []).append(r)
    if not groups:
        return []
    summary = []
    detail = []
    order = {"tps": 0, "bw": 1, "pps": 2, "cps": 3}
    for key in sorted(groups, key=lambda k: (order.get(k[3], 9), k[0], k[1], k[2])):
        topo, vx, jumbo, test = key
        rs = sorted(groups[key], key=lambda r: r["n"])
        unit = next((r["metric"]["unit"] for r in rs if _ok(r)), "")
        curve = [(r["n"], r["metric"]["value"]) for r in rs if _ok(r)]
        n_opt, v_opt, vmax, n_max = optimum(curve, tol)
        title = "%s, %s%s - %s" % (topo, vx_label(vx), ", jumbo" if jumbo else "",
                                   TEST_TITLES[1].get(test, test))
        summary.append([test, topo, vx_label(vx) + (" jumbo" if jumbo else ""),
                        "%s %s" % (fmt(vmax, unit), unit) if vmax else "-",
                        n_max or "-", n_opt or "-",
                        "%s %s" % (fmt(v_opt / n_opt, unit), unit) if n_opt else "-"])
        rows = []
        for r in rs:
            if not _ok(r):
                rows.append([r["n"], "FAILED", "", "", "", "", (r.get("error") or "")[:80].replace("|", "/")
                             .replace("\n", " ")])
                continue
            v = r["metric"]["value"]
            ex = r.get("extra", {})
            cpu = r.get("cpu", {})
            if test in ("tps", "bw"):
                detail_v = "rx %.2f / tx %.2f Gbps, %s rps" % (
                    r["client"].get("gbps_rx", 0), r["client"].get("gbps_tx", 0), fmt(ex.get("rps")))
            elif test == "pps":
                detail_v = "client tx %.2f Mpps, server rx %.2f / tx %.2f Mpps" % (
                    ex.get("client_mpps_tx", 0), ex.get("server_mpps_rx", 0), ex.get("server_mpps_tx", 0))
            else:
                detail_v = "peak %s, steady %s" % (fmt(ex.get("cps_peak")), fmt(ex.get("cps_steady")))
            mark = " **(opt)**" if r["n"] == n_opt else ""
            rows.append(["%d%s" % (r["n"], mark), "%s %s" % (fmt(v, unit), unit),
                         "%s %s" % (fmt(v / r["n"], unit), unit), detail_v,
                         fmt(cpu.get("client_dperf_pct"), "%"), fmt(cpu.get("server_dperf_pct"), "%"),
                         " ".join(r.get("flags", []))])
        detail.append("### " + title)
        detail.append("")
        detail.append(table(["cores/side", "result", "per core", "details", "client dperf CPU %",
                             "server dperf CPU %", "flags"], rows))
        detail.append("")
    L.append("Optimum = the smallest number of cores per side that reaches >= %d%% of the "
             "maximum of the curve." % round(100 * (1 - tol)))
    L.append("")
    L.append(table(["test", "topology", "encap", "max", "cores @max", "optimal cores",
                    "per core @opt"], summary))
    L.append("")
    L += detail
    return L


def s2_section(recs, tol):
    L = ["## Scenario 2: dperf -> nginx, Linux kernel vs F-Stack", ""]
    groups = {}
    for r in recs:
        if r.get("scenario") != 2:
            continue
        key = (r["topology"], r["vxlan"], r["test"], r.get("size"))
        groups.setdefault(key, {}).setdefault(r["stack"], []).append(r)
    if not groups:
        return []
    verdicts = []
    detail = []
    order = {"rps": 0, "cps": 1, "bw": 2}
    for key in sorted(groups, key=lambda k: (order.get(k[2], 9), k[0], k[1], k[3] or 0)):
        topo, vx, test, size = key
        by = groups[key]
        unit = None
        curves = {}
        effc = {}
        for stack, rs in by.items():
            rs.sort(key=lambda r: r["workers"])
            for r in rs:
                if _ok(r):
                    unit = r["metric"]["unit"]
            curves[stack] = [(r["workers"], r["metric"]["value"]) for r in rs if _ok(r)]
        opt = {s: optimum(c, tol) for s, c in curves.items()}
        workers = sorted(set(r["workers"] for rs in by.values() for r in rs))
        idx = {s: {r["workers"]: r for r in rs} for s, rs in by.items()}
        rows = []
        for w in workers:
            row = [w]
            vals = {}
            for stack in ("kernel", "fstack"):
                r = idx.get(stack, {}).get(w)
                if r is None:
                    row += ["", "", ""]
                    continue
                if not _ok(r):
                    row += ["FAILED", "", ""]
                    continue
                v = r["metric"]["value"]
                vals[stack] = v
                cpu = r.get("cpu", {})
                if stack == "kernel":
                    used = cpu.get("server_busy_cores")
                    cpu_s = "%s busy" % fmt(used, "cores")
                else:
                    used = cpu.get("fstack_effective_cores")
                    cpu_s = "%d alloc / %s busy" % (w, fmt(used, "cores"))
                eff = v / used if used else None
                effc.setdefault(stack, {})[w] = (v, used)
                flags = " ".join(r.get("flags", []))
                opt_mark = " (opt)" if opt.get(stack) and opt[stack][0] == w else ""
                row += ["%s%s%s" % (fmt(v, unit), opt_mark, (" [%s]" % flags) if flags else ""), cpu_s,
                        fmt(eff, unit)]
            if "kernel" in vals and "fstack" in vals and vals["kernel"]:
                row.append("x%.2f" % (vals["fstack"] / vals["kernel"]))
            else:
                row.append("")
            rows.append(row)
        title = "%s, %s - %s%s" % (topo, vx_label(vx), TEST_TITLES[2].get(test, test),
                                   (" (%s)" % size_label(size)) if test == "bw" else "")
        detail.append("### " + title)
        detail.append("")
        detail.append(table(["workers", "kernel " + (unit or ""), "kernel CPU (cores)",
                             "kernel per busy core", "F-Stack " + (unit or ""),
                             "F-Stack CPU (cores)", "F-Stack per busy core", "F-Stack/kernel"], rows))
        detail.append("")
        # verdict
        vk = opt.get("kernel", (None,) * 4)
        vf = opt.get("fstack", (None,) * 4)
        if vk[2] and vf[2]:
            winner = "F-Stack" if vf[2] > vk[2] else "kernel"
            ratio = vf[2] / vk[2]
            # CPU needed by F-Stack to reach the kernel's best throughput
            need_f = next((w for w, v in sorted(curves["fstack"]) if v >= vk[2] * (1 - tol)), None)
            verdicts.append([test + (" " + size_label(size) if test == "bw" else ""), topo, vx_label(vx),
                             "%s %s @%dw" % (fmt(vk[2], unit), unit, vk[3]),
                             "%s %s @%dw" % (fmt(vf[2], unit), unit, vf[3]),
                             "x%.2f (%s)" % (ratio, winner),
                             "%s / %s" % (vk[0], vf[0]),
                             "%s" % (need_f if need_f else "never")])
    L.append("Kernel CPU = busy CPU time of the whole machine during the window "
             "(user+sys+irq+softirq, in cores). F-Stack CPU: 'alloc' = polling cores taken "
             "(100%% busy for the OS), 'busy' = useful work reported by ff_top (sys+usr). "
             "Optimum = fewest workers reaching >= %d%% of the stack's own maximum." %
             round(100 * (1 - tol)))
    L.append("")
    if verdicts:
        L.append(table(["test", "topology", "encap", "kernel max", "F-Stack max",
                        "F-Stack/kernel", "optimal workers (kernel / F-Stack)",
                        "F-Stack workers to match kernel max"], verdicts))
        L.append("")
    L += detail
    return L


def host_section(meta):
    L = []
    for role in ("client", "server"):
        hi = (meta.get("hosts") or {}).get(role)
        if not hi:
            continue
        L.append("- **%s** `%s`: %s, kernel %s, %s CPUs, governor %s" % (
            role, hi.get("hostname"), hi.get("cpu_model"), hi.get("kernel"), hi.get("online_cpus"),
            hi.get("governor") or "-"))
        for n in hi.get("nics", []):
            L.append("  - NIC %s: %s %s fw %s, NUMA %s, speed %s" % (
                n.get("pci"), n.get("driver"), n.get("version", ""), n.get("firmware-version", "-"),
                n.get("numa"), n.get("speed", "-")))
        for w in hi.get("warnings", []):
            L.append("  - warning: %s" % w)
    return L


def generate(run_dir, tol=0.05):
    recs, meta = load(run_dir)
    L = ["# fbench report", ""]
    if meta:
        L.append("Run `%s`, started %s, config digest `%s`." % (
            os.path.basename(run_dir.rstrip("/")), meta.get("started_h", "?"), meta.get("digest", "?")))
        L.append("")
        L += host_section(meta)
        L.append("")
    ok = sum(1 for r in recs if r.get("status") == "ok")
    L.append("Points: %d ok, %d failed." % (ok, len(recs) - ok))
    L.append("")
    L += s1_section(recs, tol)
    L += s2_section(recs, tol)
    failed = [r for r in recs if r.get("status") != "ok"]
    if failed:
        L.append("## Failed points")
        L.append("")
        for r in failed:
            L.append("- `%s`: %s" % (r["id"], (r.get("error") or "").splitlines()[0][:300]
                                      if r.get("error") else "?"))
        L.append("")
    md = "\n".join(L) + "\n"
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write(md)
    write_csv(recs, os.path.join(run_dir, "summary.csv"))
    return os.path.join(run_dir, "report.md")


def write_csv(recs, path):
    cols = ["id", "scenario", "stack", "topology", "vxlan", "jumbo", "test", "size", "n", "workers",
            "status", "metric", "unit", "gbps_rx", "gbps_tx", "mpps_rx", "mpps_tx", "rps",
            "client_cpu_pct", "client_max_worker_pct", "server_busy_cores",
            "fstack_effective_cores", "server_dperf_pct", "flags", "flow", "error"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(recs, key=lambda x: x["id"]):
            c = r.get("client") or {}
            cpu = r.get("cpu") or {}
            m = r.get("metric") or {}
            w.writerow([r.get("id"), r.get("scenario"), r.get("stack", "dperf"), r.get("topology"),
                        r.get("vxlan"), r.get("jumbo", ""), r.get("test"), r.get("size", ""),
                        r.get("n", ""), r.get("workers", ""), r.get("status"), m.get("value"),
                        m.get("unit"), c.get("gbps_rx"), c.get("gbps_tx"), c.get("mpps_rx"),
                        c.get("mpps_tx"), c.get("rsp"),
                        cpu.get("client_dperf_pct"), cpu.get("client_max_worker_pct"),
                        cpu.get("server_busy_cores"), cpu.get("fstack_effective_cores"),
                        cpu.get("server_dperf_pct"), " ".join(r.get("flags", [])), r.get("flow"),
                        (r.get("error") or "").splitlines()[0][:200] if r.get("error") else ""])
