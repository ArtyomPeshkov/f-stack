#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>

#include "ff_ipc.h"

static void
usage(void)
{
    printf("Usage:\n");
    printf("  mem [-p <f-stack proc_id>] [-P <max proc_id>] [-s]\n");
    printf("    -p  first f-stack proc_id to ask, default 0\n");
    printf("    -P  last f-stack proc_id to ask, default same as -p\n");
    printf("    -s  print one csv line per proc, instead of a table\n");
}

static int
mem_status(struct ff_mem_args *mem)
{
    int ret;
    struct ff_msg *msg, *retmsg = NULL;

    msg = ff_ipc_msg_alloc();
    if (msg == NULL) {
        errno = ENOMEM;
        return -1;
    }

    msg->msg_type = FF_MEM;
    ret = ff_ipc_send(msg);
    if (ret < 0) {
        errno = EPIPE;
        ff_ipc_msg_free(msg);
        return -1;
    }

    do {
        if (retmsg != NULL) {
            ff_ipc_msg_free(retmsg);
        }

        ret = ff_ipc_recv(&retmsg, msg->msg_type);
        if (ret < 0) {
            errno = EPIPE;
            return -1;
        }
    } while (msg != retmsg);

    *mem = retmsg->mem;

    ff_ipc_msg_free(msg);

    return 0;
}

#define TO_MB(bytes) ((double)(bytes) / (1024 * 1024))

int
main(int argc, char **argv)
{
    int ch, single = 0;
    int proc_id = 0, max_proc_id = -1, i;

    ff_ipc_init();

    while ((ch = getopt(argc, argv, "hp:P:s")) != -1) {
        switch(ch) {
        case 'p':
            proc_id = atoi(optarg);
            break;
        case 'P':
            max_proc_id = atoi(optarg);
            break;
        case 's':
            single = 1;
            break;
        case 'h':
        default:
            usage();
            ff_ipc_exit();
            return -1;
        }
    }

    if (proc_id < 0 || proc_id >= RTE_MAX_LCORE ||
        max_proc_id >= RTE_MAX_LCORE ||
        (max_proc_id != -1 && max_proc_id < proc_id)) {
        usage();
        ff_ipc_exit();
        return -1;
    }

    if (max_proc_id == -1) {
        max_proc_id = proc_id;
    }

    if (!single) {
        printf("|---------|--------|--------------|--------------|"
            "--------------|--------------|------------|------------|\n");
        printf("|%9s|%8s|%14s|%14s|%14s|%14s|%12s|%12s|\n",
            "proc_id", "socket", "hugepage(MB)", "heap(MB)",
            "heap used(MB)", "heap free(MB)", "mbuf total", "mbuf inuse");
        printf("|---------|--------|--------------|--------------|"
            "--------------|--------------|------------|------------|\n");
    }

    for (i = proc_id; i <= max_proc_id; i++) {
        struct ff_mem_args mem;

        ff_set_proc_id(i);
        if (mem_status(&mem)) {
            printf("fstack ipc message error, proc id:%d!\n", i);
            ff_ipc_exit();
            return -1;
        }

        if (single) {
            printf("%d,%d,%lu,%lu,%lu,%lu,%u,%u\n",
                i, mem.socket_id, mem.hugepage_bytes,
                mem.heap_total_bytes, mem.heap_alloc_bytes,
                mem.heap_free_bytes, mem.mbuf_total, mem.mbuf_inuse);
        } else {
            printf("|%9d|%8d|%14.2f|%14.2f|%14.2f|%14.2f|%12u|%12u|\n",
                i, mem.socket_id, TO_MB(mem.hugepage_bytes),
                TO_MB(mem.heap_total_bytes), TO_MB(mem.heap_alloc_bytes),
                TO_MB(mem.heap_free_bytes), mem.mbuf_total, mem.mbuf_inuse);
        }
    }

    if (!single) {
        printf("|---------|--------|--------------|--------------|"
            "--------------|--------------|------------|------------|\n");
        printf("hugepage/heap/mbuf memory is shared by all f-stack "
            "processes of the same socket, do not sum it up.\n");
    }

    ff_ipc_exit();
    return 0;
}
