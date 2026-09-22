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
 * ARP ping helpers, used by the `arpping' tool through the FF_ARPPING ipc
 * message and directly callable by the application, e.g. to announce a
 * virtual address right after it has been taken over.
 */

#include <sys/param.h>
#include <sys/systm.h>
#include <sys/kernel.h>
#include <sys/socket.h>
#include <sys/ck.h>
#include <sys/epoch.h>

#include <net/if.h>
#include <net/if_var.h>
#include <net/if_dl.h>
#include <net/if_llatbl.h>
#include <net/if_types.h>
#include <net/ethernet.h>
#include <net/vnet.h>

#include <netinet/in.h>
#include <netinet/in_var.h>
#include <netinet/if_ether.h>

#include "ff_api.h"

/*
 * Return a referenced ifnet, the caller must release it with if_rele().
 * An empty name selects the first running ethernet interface, which is
 * what every single port setup wants.
 */
static struct ifnet *
ff_arpping_ifp(const char *ifname)
{
    struct epoch_tracker et;
    struct ifnet *ifp;

    if (ifname != NULL && ifname[0] != '\0') {
        return (ifunit_ref(ifname));
    }

    NET_EPOCH_ENTER(et);
    CK_STAILQ_FOREACH(ifp, &V_ifnet, if_link) {
        if (ifp->if_type == IFT_ETHER && (ifp->if_flags & IFF_UP) &&
            !(ifp->if_flags & IFF_DYING)) {
            if_ref(ifp);
            break;
        }
    }
    NET_EPOCH_EXIT(et);

    return (ifp);
}

static void
ff_arpping_sin(struct sockaddr_in *sin, uint32_t addr)
{
    bzero(sin, sizeof(*sin));
    sin->sin_len = sizeof(*sin);
    sin->sin_family = AF_INET;
    sin->sin_addr.s_addr = addr;
}

/*
 * Read the ARP entry of `tip' on `ifp'. `mac' may be NULL when only the
 * flags are of interest.
 */
static void
ff_arpping_entry(struct ifnet *ifp, uint32_t tip, uint8_t *mac, int *lle_flags)
{
    struct epoch_tracker et;
    struct llentry *lle;
    struct sockaddr_in sin;

    *lle_flags = 0;

    ff_arpping_sin(&sin, tip);

    NET_EPOCH_ENTER(et);
    lle = lla_lookup(LLTABLE(ifp), LLE_UNLOCKED, (struct sockaddr *)&sin);
    if (lle != NULL) {
        if (lle->la_flags & LLE_VALID) {
            *lle_flags |= FF_ARPPING_LLE_VALID;
            if (mac != NULL) {
                bcopy(lle->ll_addr, mac, ETHER_ADDR_LEN);
            }
        }
        if (lle->la_flags & LLE_STATIC) {
            *lle_flags |= FF_ARPPING_LLE_STATIC;
        }
        if (lle->la_flags & LLE_IFADDR) {
            *lle_flags |= FF_ARPPING_LLE_LOCAL;
        }
    }
    NET_EPOCH_EXIT(et);
}

int
ff_arpping_request(const char *ifname, uint32_t sip, uint32_t tip, int flush)
{
    struct epoch_tracker et;
    struct ifnet *ifp;
    struct sockaddr_in sin;
    struct in_addr src, dst;
    int lle_flags;

    if (tip == INADDR_ANY) {
        return (-EINVAL);
    }

    ifp = ff_arpping_ifp(ifname);
    if (ifp == NULL) {
        return (-ENXIO);
    }

    if (ifp->if_addrlen != ETHER_ADDR_LEN) {
        if_rele(ifp);
        return (-EOPNOTSUPP);
    }

    /*
     * Forget what we know about the target, so that the caller can tell a
     * fresh reply from a cache entry that was already there. An entry that
     * was configured by hand is left alone, dropping it would turn a probe
     * into a change of the configuration.
     */
    if (flush) {
        ff_arpping_entry(ifp, tip, NULL, &lle_flags);

        if ((lle_flags &
            (FF_ARPPING_LLE_STATIC | FF_ARPPING_LLE_LOCAL)) == 0) {
            ff_arpping_sin(&sin, tip);
            lltable_delete_addr(LLTABLE(ifp), 0, (struct sockaddr *)&sin);
        }
    }

    src.s_addr = sip;
    dst.s_addr = tip;

    NET_EPOCH_ENTER(et);
    arprequest(ifp, sip == INADDR_ANY ? NULL : &src, &dst, NULL);
    NET_EPOCH_EXIT(et);

    if_rele(ifp);

    return (0);
}

int
ff_arpping_lookup(const char *ifname, uint32_t tip, uint8_t *mac,
    int *lle_flags)
{
    struct ifnet *ifp;

    if (mac == NULL || lle_flags == NULL) {
        return (-EINVAL);
    }

    *lle_flags = 0;

    ifp = ff_arpping_ifp(ifname);
    if (ifp == NULL) {
        return (-ENXIO);
    }

    if (ifp->if_addrlen != ETHER_ADDR_LEN) {
        if_rele(ifp);
        return (-EOPNOTSUPP);
    }

    ff_arpping_entry(ifp, tip, mac, lle_flags);

    if_rele(ifp);

    return (0);
}

int
ff_arpping_announce(const char *ifname, uint32_t ip)
{
    /*
     * Gratuitous ARP: an unsolicited request whose sender and target
     * protocol addresses are both ours, see RFC 5227. This is the same
     * packet the stack sends when an address is configured.
     */
    return (ff_arpping_request(ifname, ip, ip, 0));
}
