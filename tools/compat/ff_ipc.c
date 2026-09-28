/*
 * Copyright (C) 2017-2021 THL A29 Limited, a Tencent company.
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

#include <rte_common.h>
#include <rte_memory.h>
#include <rte_config.h>
#include <rte_eal.h>
#include <rte_ring.h>
#include <rte_mempool.h>
#include <rte_malloc.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

#include "ff_ipc.h"

static int inited;

static struct rte_mempool *message_pool;

uint16_t ff_proc_id = 0;

void
ff_set_proc_id(int pid)
{
    if (pid < 0 || pid > 65535) {
        printf("Invalid F-Stack proccess id\n");
        exit(1);
    }
    ff_proc_id = pid;
}

int
ff_ipc_init(void)
{
    if (inited) {
        return 0;
    }

    if (getuid() != 0) {
        rte_exit(EXIT_FAILURE, "Error: F-Stack tools must be run as root.\n");
    }

    char *dpdk_argv[] = {
        "ff-ipc", "-c1", "-n4",
        "--proc-type=secondary",
        /* RTE_LOG_WARNING */
        "--log-level=5",
    };

    int ret = rte_eal_init(sizeof(dpdk_argv)/sizeof(dpdk_argv[0]), dpdk_argv);
    if (ret < 0) {
        rte_exit(EXIT_FAILURE, "Error with EAL initialization\n");
    }

    message_pool = rte_mempool_lookup(FF_MSG_POOL);
    if (message_pool == NULL) {
        rte_exit(EXIT_FAILURE, "lookup message pool:%s failed!\n", FF_MSG_POOL);
    }

    inited = 1;

    return 0;
}

void
ff_ipc_exit(void)
{
	rte_eal_cleanup();
	return;
}

struct ff_msg *
ff_ipc_msg_alloc(void)
{
    if (inited == 0) {
        int ret = ff_ipc_init();
        if (ret < 0) {
            return NULL;
        }
    }

    /*
     * Every tool runs as lcore 0 of its own secondary process, so the
     * per-lcore cache would be shared by concurrent tools without any
     * locking: bypass it, the pool's ring is multi-process safe.
     */
    void *msg;
    if (rte_mempool_generic_get(message_pool, &msg, 1, NULL) < 0) {
        printf("get buffer from message pool failed.\n");
        return NULL;
    }

    return (struct ff_msg *)msg;
}

int
ff_ipc_msg_free(struct ff_msg *msg)
{
    if (inited == 0) {
        printf("ff ipc not inited\n");
        return -1;
    }

    if (msg->original_buf) {
        rte_free(msg->buf_addr);
        msg->buf_addr = msg->original_buf;
        msg->buf_len = msg->original_buf_len;
        msg->original_buf = NULL;
    }

    rte_mempool_generic_put(message_pool, (void **)&msg, 1, NULL);

    return 0;
}

static int
ff_ipc_send(const struct ff_msg *msg)
{
    int ret;

    if (inited == 0) {
        printf("ff ipc not inited\n");
        return -1;
    }

    char name[RTE_RING_NAMESIZE];
    snprintf(name, RTE_RING_NAMESIZE, "%s%u",
        FF_MSG_RING_IN, ff_proc_id);
    struct rte_ring *ring = rte_ring_lookup(name);
    if (ring == NULL) {
        printf("lookup message ring:%s failed!\n", name);
        return -1;
    }

    ret = rte_ring_enqueue(ring, (void *)msg);
    if (ret < 0) {
        printf("ff_ipc_send failed\n");
        return ret;
    }

    return 0;
}

static int
ff_ipc_recv(struct ff_msg **msg, enum FF_MSG_TYPE msg_type)
{
    int ret, i;
    if (inited == 0) {
        printf("ff ipc not inited\n");
        return -1;
    }

    char name[RTE_RING_NAMESIZE];
    snprintf(name, RTE_RING_NAMESIZE, "%s%u_%u",
        FF_MSG_RING_OUT, ff_proc_id, msg_type);
    struct rte_ring *ring = rte_ring_lookup(name);
    if (ring == NULL) {
        printf("lookup message ring:%s failed!\n", name);
        return -1;
    }

    void *obj;
    #define MAX_ATTEMPTS_NUM 1000
    for (i = 0; i < MAX_ATTEMPTS_NUM; i++) {
        ret = rte_ring_dequeue(ring, &obj);
        if (ret == 0) {
            *msg = (struct ff_msg *)obj;
            break;
        }

        usleep(1000);
    }

    return ret;
}

/*
 * Tools are separate DPDK secondary processes sharing the msg rings of an
 * F-Stack process: the in ring is single-producer, every out ring is
 * single-consumer, and a reply can only be matched to its request by
 * whoever dequeues it. So only one request per F-Stack process may be in
 * flight at a time. That is enforced with flock() on a per-process lock
 * file: the kernel drops it if the holder dies, and every call opens its
 * own file description, so it serializes threads as well as processes.
 */
static int
ff_ipc_lock(void)
{
    char path[PATH_MAX];
    int fd;

    snprintf(path, sizeof(path), "%s/ff_ipc_%u.lock",
        rte_eal_get_runtime_dir(), ff_proc_id);

    fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        printf("open %s failed: %s\n", path, strerror(errno));
        return -1;
    }

    while (flock(fd, LOCK_EX) < 0) {
        if (errno != EINTR) {
            printf("flock %s failed: %s\n", path, strerror(errno));
            close(fd);
            return -1;
        }
    }

    return fd;
}

int
ff_ipc_call(struct ff_msg *msg)
{
    enum FF_MSG_TYPE msg_type = msg->msg_type;
    struct ff_msg *retmsg;
    int fd, ret;

    if (inited == 0) {
        printf("ff ipc not inited\n");
        errno = EPIPE;
        return -1;
    }

    fd = ff_ipc_lock();
    if (fd < 0) {
        ff_ipc_msg_free(msg);
        errno = EPIPE;
        return -1;
    }

    ret = ff_ipc_send(msg);
    if (ret < 0) {
        close(fd);
        ff_ipc_msg_free(msg);
        errno = EPIPE;
        return -1;
    }

    for (;;) {
        ret = ff_ipc_recv(&retmsg, msg_type);
        if (ret < 0) {
            /*
             * Timed out: msg is still in flight, its late reply will be
             * freed by the next call that dequeues it below.
             */
            close(fd);
            errno = EPIPE;
            return -1;
        }

        if (retmsg == msg) {
            break;
        }

        /* Late reply to an earlier call that timed out. */
        ff_ipc_msg_free(retmsg);
    }

    /* Closing the only descriptor of the lock file releases the lock. */
    close(fd);

    return 0;
}
