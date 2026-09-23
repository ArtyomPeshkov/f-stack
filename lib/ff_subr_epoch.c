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
 */

#include <sys/cdefs.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/kernel.h>
#include <sys/ktr.h>
#include <sys/limits.h>
#include <sys/lock.h>
#include <sys/mutex.h>
#include <sys/proc.h>
#include <sys/malloc.h>
#include <sys/resourcevar.h>
#include <sys/sched.h>
#include <sys/sysctl.h>
#include <sys/uio.h>
#include <sys/epoch.h>

#include "ff_host_interface.h"

struct epoch {
};

static struct epoch epoch_array[1];

/*
 * F-Stack runs the whole stack on one thread per process, so there are no
 * concurrent epoch readers.  FreeBSD code still relies on epoch_call() to
 * keep an unlinked object readable until the current traversal finishes,
 * e.g. in6m_disconnect_locked() freeing ll_ifma from inside the
 * CK_STAILQ_FOREACH over if_multiaddrs in mld_fasttimo_vnet().  Running the
 * callback immediately turns that into a use-after-free.
 *
 * Queue the callbacks instead and run them from the main loop via
 * ff_epoch_run_callbacks(), where no stack code is on the call stack.
 * The queue linkage reuses the epoch_context embedded in each object.
 */
struct ff_epoch_cb {
    epoch_callback_t   *cb;
    struct ff_epoch_cb *next;
};
CTASSERT(sizeof(struct ff_epoch_cb) <= sizeof(struct epoch_context));

static struct ff_epoch_cb *ff_epoch_head;
static struct ff_epoch_cb **ff_epoch_tailp = &ff_epoch_head;

void
_epoch_enter_preempt(epoch_t epoch, epoch_tracker_t et EPOCH_FILE_LINE)
{

}

void
_epoch_exit_preempt(epoch_t epoch, epoch_tracker_t et EPOCH_FILE_LINE)
{

}

void
epoch_wait_preempt(epoch_t epoch)
{

}

void
epoch_call(epoch_t epoch, epoch_callback_t callback, epoch_context_t ctx)
{
    struct ff_epoch_cb *e = (struct ff_epoch_cb *)ctx;

    e->cb = callback;
    e->next = NULL;
    *ff_epoch_tailp = e;
    ff_epoch_tailp = &e->next;
}

void
ff_epoch_run_callbacks(void)
{
    struct ff_epoch_cb *e, *next;

    /* Callbacks may call epoch_call() again, so loop until empty. */
    while ((e = ff_epoch_head) != NULL) {
        ff_epoch_head = NULL;
        ff_epoch_tailp = &ff_epoch_head;
        for (; e != NULL; e = next) {
            /* Read next before the callback frees the object. */
            next = e->next;
            e->cb((epoch_context_t)e);
        }
    }
}

epoch_t
epoch_alloc(const char *name, int flags)
{
    return &epoch_array[0];
}

void
epoch_drain_callbacks(epoch_t epoch)
{
    ff_epoch_run_callbacks();
}
