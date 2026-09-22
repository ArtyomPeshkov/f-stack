/*
 * Copyright (C) 2026 The F-Stack Authors.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice, this
 *   list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *   this list of conditions and the following disclaimer in the documentation
 *   and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
 * ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 * SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 */

#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "ff_api.h"
#include "ff_ipc.h"

/*
 * How often the ARP table of the F-Stack process is polled while waiting
 * for a reply. It is the resolution of the round trip time we report.
 */
#define POLL_INTERVAL_US    500

/* Granularity of the interruptible sleep between two probes. */
#define NAP_CHUNK_US        50000

static const char *progname = "arpping";

static int quiet;
static int finish_on_first;
static int dad_mode;
static int announce_mode;
static int keep_cache;

static volatile sig_atomic_t interrupted;

static unsigned long sent;
static unsigned long received;
static double rtt_min = -1.0;
static double rtt_max;
static double rtt_sum;

static void
usage(void)
{
    fprintf(stderr,
        "usage: %s -p <f-stack proc_id> [-bfqDKU] [-c count] [-i interval]\n"
        "                  [-w deadline] [-I interface] [-s source] destination\n"
        "\n"
        "  -c count      stop after <count> probes\n"
        "  -i interval   seconds between two probes, default 1\n"
        "  -w deadline   give up after <deadline> seconds\n"
        "  -I interface  interface to use, default the first running one\n"
        "  -s source     sender address of the request\n"
        "  -D            duplicate address detection, exit 1 if the address is in use\n"
        "  -U            send a gratuitous ARP announcing <destination> instead\n"
        "  -K            keep the ARP entry instead of dropping it before each probe\n"
        "  -f            exit on the first reply\n"
        "  -q            quiet, print the summary only\n"
        "  -b            accepted for compatibility, requests are always broadcast\n",
        progname);
}

static void
on_interrupt(int sig)
{
    (void)sig;
    interrupted = 1;
}

static double
now_sec(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);

    return ts.tv_sec + ts.tv_nsec / 1000000000.0;
}

static const char *
ip_str(uint32_t addr)
{
    static char buf[INET_ADDRSTRLEN];

    if (inet_ntop(AF_INET, &addr, buf, sizeof(buf)) == NULL) {
        return "?";
    }

    return buf;
}

static const char *
mac_str(const uint8_t *mac)
{
    static char buf[18];

    snprintf(buf, sizeof(buf), "%02x:%02x:%02x:%02x:%02x:%02x",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    return buf;
}

static int
parse_addr(const char *str, uint32_t *addr)
{
    struct addrinfo hints, *res;
    struct in_addr in;

    if (inet_pton(AF_INET, str, &in) == 1) {
        *addr = in.s_addr;
        return 0;
    }

    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;

    if (getaddrinfo(str, NULL, &hints, &res) != 0) {
        return -1;
    }

    *addr = ((struct sockaddr_in *)(void *)res->ai_addr)->sin_addr.s_addr;
    freeaddrinfo(res);

    return 0;
}

/*
 * Send one request to the F-Stack process and wait for its answer.
 * The reply overwrites `args', errno is set from the result of the
 * message on failure.
 */
static int
arpping_ipc(struct ff_arpping_args *args)
{
    struct ff_msg *msg, *retmsg = NULL;
    int ret;

    msg = ff_ipc_msg_alloc();
    if (msg == NULL) {
        errno = ENOMEM;
        return -1;
    }

    msg->msg_type = FF_ARPPING;
    msg->arpping = *args;

    ret = ff_ipc_send(msg);
    if (ret < 0) {
        ff_ipc_msg_free(msg);
        errno = EPIPE;
        return -1;
    }

    do {
        if (retmsg != NULL) {
            ff_ipc_msg_free(retmsg);
        }

        ret = ff_ipc_recv(&retmsg, FF_ARPPING);
        if (ret < 0) {
            ff_ipc_msg_free(msg);
            errno = EPIPE;
            return -1;
        }
    } while (msg != retmsg);

    *args = retmsg->arpping;
    ret = retmsg->result;

    ff_ipc_msg_free(msg);

    if (ret != 0) {
        errno = ret;
        return -1;
    }

    return 0;
}

/* Sleep up to `sec' seconds, returning early when interrupted. */
static void
nap(double sec)
{
    double deadline = now_sec() + sec;

    while (!interrupted) {
        double left = deadline - now_sec();

        if (left <= 0) {
            break;
        }

        usleep(left * 1000000.0 > NAP_CHUNK_US ?
            NAP_CHUNK_US : (useconds_t)(left * 1000000.0));
    }
}

/*
 * Send one ARP request and wait up to `wait' seconds for the entry of the
 * target to show up. Returns 1 when a reply was seen, 0 on timeout and -1
 * when the F-Stack process refused the request.
 */
static int
probe_once(const struct ff_arpping_args *tmpl, double wait)
{
    struct ff_arpping_args args;
    double start, rtt;

    args = *tmpl;
    args.cmd = FF_ARPPING_CMD_PROBE;
    args.flags = keep_cache ? 0 : FF_ARPPING_FLAG_FLUSH;

    start = now_sec();

    if (arpping_ipc(&args) < 0) {
        fprintf(stderr, "%s: cannot send an ARP request for %s: %s\n",
            progname, ip_str(tmpl->target_ip), strerror(errno));
        return -1;
    }

    sent++;

    while (!interrupted) {
        args = *tmpl;
        args.cmd = FF_ARPPING_CMD_POLL;

        if (arpping_ipc(&args) < 0) {
            fprintf(stderr, "%s: cannot read the ARP table: %s\n",
                progname, strerror(errno));
            return -1;
        }

        if (args.lle_flags & FF_ARPPING_LLE_LOCAL) {
            fprintf(stderr, "%s: %s is an address of the interface itself, "
                "it does not answer its own requests\n",
                progname, ip_str(tmpl->target_ip));
            return -1;
        }

        if (args.lle_flags & FF_ARPPING_LLE_STATIC) {
            fprintf(stderr, "%s: %s has a static ARP entry, a reply cannot "
                "be told from it, remove it with `arp -d' first\n",
                progname, ip_str(tmpl->target_ip));
            return -1;
        }

        if (args.lle_flags & FF_ARPPING_LLE_VALID) {
            rtt = (now_sec() - start) * 1000.0;

            received++;
            rtt_sum += rtt;
            if (rtt_min < 0 || rtt < rtt_min) {
                rtt_min = rtt;
            }
            if (rtt > rtt_max) {
                rtt_max = rtt;
            }

            if (!quiet) {
                printf("Reply from %s", ip_str(tmpl->target_ip));
                printf(" [%s]  %.3fms\n", mac_str(args.mac), rtt);
            }

            return 1;
        }

        if (now_sec() - start >= wait) {
            break;
        }

        usleep(POLL_INTERVAL_US);
    }

    return 0;
}

static int
announce_once(const struct ff_arpping_args *tmpl)
{
    struct ff_arpping_args args;

    args = *tmpl;
    args.cmd = FF_ARPPING_CMD_ANNOUNCE;

    if (arpping_ipc(&args) < 0) {
        fprintf(stderr, "%s: cannot announce %s: %s\n",
            progname, ip_str(tmpl->target_ip), strerror(errno));
        return -1;
    }

    sent++;

    if (!quiet) {
        printf("Announced %s\n", ip_str(tmpl->target_ip));
    }

    return 0;
}

static void
report(uint32_t target)
{
    printf("\n--- %s arpping statistics ---\n", ip_str(target));
    printf("%lu probes transmitted, %lu responses received, %.0f%% loss\n",
        sent, received,
        sent ? (double)(sent - received) * 100.0 / (double)sent : 0.0);

    if (received != 0) {
        printf("rtt min/avg/max = %.3f/%.3f/%.3f ms\n",
            rtt_min, rtt_sum / (double)received, rtt_max);
    }
}

int
main(int argc, char **argv)
{
    struct ff_arpping_args tmpl;
    double interval = 1.0, deadline = 0.0, start, wait, cycle;
    long count = -1, n;
    int ch, proc_id = 0, ret;

    memset(&tmpl, 0, sizeof(tmpl));

    while ((ch = getopt(argc, argv, "p:c:i:w:I:s:AbDfhqKU")) != -1) {
        switch (ch) {
        case 'p':
            proc_id = atoi(optarg);
            break;
        case 'c':
            count = strtol(optarg, NULL, 10);
            if (count <= 0) {
                fprintf(stderr, "%s: invalid count %s\n", progname, optarg);
                return 2;
            }
            break;
        case 'i':
            interval = atof(optarg);
            if (interval < 0) {
                fprintf(stderr, "%s: invalid interval %s\n", progname, optarg);
                return 2;
            }
            break;
        case 'w':
            deadline = atof(optarg);
            if (deadline < 0) {
                fprintf(stderr, "%s: invalid deadline %s\n", progname, optarg);
                return 2;
            }
            break;
        case 'I':
            snprintf(tmpl.ifname, sizeof(tmpl.ifname), "%s", optarg);
            break;
        case 's':
            if (parse_addr(optarg, &tmpl.source_ip) < 0) {
                fprintf(stderr, "%s: unknown source %s\n", progname, optarg);
                return 2;
            }
            break;
        case 'D':
            dad_mode = 1;
            break;
        case 'U':
            announce_mode = 1;
            break;
        case 'A':
            fprintf(stderr, "%s: -A is not supported yet, use -U to send "
                "a gratuitous ARP request\n", progname);
            return 2;
        case 'b':
            /* Requests always go out as broadcast, nothing to do. */
            break;
        case 'f':
            finish_on_first = 1;
            break;
        case 'q':
            quiet = 1;
            break;
        case 'K':
            keep_cache = 1;
            break;
        case 'h':
        default:
            usage();
            return 2;
        }
    }

    argc -= optind;
    argv += optind;

    if (argc != 1) {
        usage();
        return 2;
    }

    if (parse_addr(argv[0], &tmpl.target_ip) < 0) {
        fprintf(stderr, "%s: unknown host %s\n", progname, argv[0]);
        return 2;
    }

    if (dad_mode && announce_mode) {
        fprintf(stderr, "%s: -D and -U are mutually exclusive\n", progname);
        return 2;
    }

    /* A single packet is enough to tell whether an address is taken. */
    if (count < 0 && (dad_mode || announce_mode)) {
        count = 1;
    }

    signal(SIGINT, on_interrupt);

    ff_ipc_init();
    ff_set_proc_id(proc_id);

    if (!quiet) {
        printf("ARPPING %s", ip_str(tmpl.target_ip));
        printf(" from %s on %s\n",
            tmpl.source_ip ? ip_str(tmpl.source_ip) : "the interface address",
            tmpl.ifname[0] ? tmpl.ifname : "the first running interface");
    }

    ret = 0;
    start = now_sec();

    for (n = 0; (count < 0 || n < count) && !interrupted; n++) {
        cycle = now_sec();

        if (announce_mode) {
            if (announce_once(&tmpl) < 0) {
                ret = 2;
                break;
            }
        } else {
            wait = interval;

            if (deadline > 0) {
                double left = deadline - (now_sec() - start);

                if (left <= 0) {
                    break;
                }
                if (wait > left) {
                    wait = left;
                }
            }

            switch (probe_once(&tmpl, wait)) {
            case -1:
                ret = 2;
                break;
            case 1:
                if (finish_on_first) {
                    goto done;
                }
                break;
            default:
                break;
            }

            if (ret != 0) {
                break;
            }
        }

        if (deadline > 0 && now_sec() - start >= deadline) {
            break;
        }

        if (count < 0 || n + 1 < count) {
            nap(interval - (now_sec() - cycle));
        }
    }

done:
    if (!quiet && !announce_mode && ret == 0) {
        report(tmpl.target_ip);
    }

    ff_ipc_exit();

    if (ret != 0) {
        return ret;
    }

    if (announce_mode) {
        return 0;
    }

    /* In DAD mode a reply means that somebody else owns the address. */
    if (dad_mode) {
        return received != 0 ? 1 : 0;
    }

    return received != 0 ? 0 : 1;
}
