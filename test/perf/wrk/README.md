# wrk

Load generator setup for F-Stack performance testing.

Status: **stage 1a/1b complete** — build and smoke test. Workload scripts
(1c) and the pure-TCP question (1d) are not in this directory yet.

## What wrk is and is not

wrk is an **HTTP-only** benchmarking tool. It cannot benchmark a non-HTTP
protocol — see "Pure TCP" below before planning around it.

It is closed-loop: each connection sends its next request only after the
previous response arrives. That makes its throughput numbers sound and its
**latency numbers optimistic** under saturation (coordinated omission). Use
it for throughput and for smoke tests; use `wrk2` at a fixed rate when the
percentiles are the result you care about.

## 1a. Build

```sh
./build.sh                 # builds into ./wrk-build/wrk/wrk
./build.sh /opt/wrk        # or somewhere else
```

wrk vendors LuaJIT and OpenSSL under `deps/`, so the host needs only a C
toolchain plus `unzip`, `tar`, `perl` and `git`. The build takes a few
minutes, most of it OpenSSL.

The resulting binary links only libc/libm/libgcc, so it can be copied
straight to a load generator host of the same architecture.

Verified with wrk 4.2.0 `[epoll]` on Ubuntu 24.04 / gcc 13.

## 1b. Verify the generator before trusting it

Never point a freshly built generator at the system under test and believe
the first number. Confirm it works against a known target first.

```sh
# on the target host
mkdir -p logs
nginx -p $(pwd) -c /path/to/nginx-target.conf

# from the generator host
./wrk -t2 -c50 -d10s --latency http://<target>:8080/600
```

`nginx-target.conf` serves three inline bodies — 10, 600 and 3700 bytes —
matching the request shapes in the F-Stack README. Bodies are returned from
the config rather than from disk so no filesystem I/O enters the path.

A healthy run reports a request rate, a transfer rate, and a latency
distribution with no `Socket errors` line. Errors here mean the target,
the host limits, or the network — not F-Stack.

### Host limits that will cap you before F-Stack does

On the **generator** host, wrk needs file descriptors and ephemeral ports:

```sh
ulimit -n 1048576
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1
```

A single generator host has ~64k ephemeral ports per destination
(IP, port) pair. For connection-heavy tests that ceiling arrives fast —
add generator IPs or generator hosts, not threads.

## Pure TCP

wrk cannot do it. Verified in `src/wrk.c`: every received byte is fed to a
strict HTTP response parser, and any parse failure tears the connection
down as a read error —

```c
if (http_parser_execute(&c->parser, &parser_settings, c->buf, n) != n) goto error;
```

Request bytes are arbitrary (a Lua `request()` may return any string), but
responses must parse as HTTP/1.x or nothing is counted. Benchmarking a
custom binary protocol with wrk is not possible without patching it.

Covered in stage 1d along with the tool to use instead.
