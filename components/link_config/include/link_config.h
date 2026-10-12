/*
 * Per-link radio identity.
 *
 * One link is one transmitter and one receiver in one case, one link per
 * room. The transmitter of each link sends from its own source address and
 * the receiver uses only CSI from that address, so links on the same channel
 * do not mix. A receiver that listens from outside the room (main unit) is
 * built with the link ID of the room it listens to.
 *
 * The link is chosen at build time and each link gets its own build
 * directory:
 *
 *     idf.py -DCSI_LINK_ID=1 -B build/link1 build
 *
 * Link 0 is the link that recorded all existing sessions; its addresses and
 * channel must not change (the channel width did, see
 * LINK_WIFI_BANDWIDTH_MHZ).
 *
 * The main unit (csi-main) sits outside the rooms. Every receiver reports its
 * 1 s decisions to it, and it listens to the transmitters itself.
 */
#pragma once

/* Base MAC of the main unit board: destination of the receivers' reports. */
#define LINK_MAIN_MAC {0x10, 0xbd, 0xa3, 0xd6, 0x74, 0x64}

/* Every transmitter address is this prefix followed by the link ID, which is
 * how the main unit tells the links apart. */
#define LINK_TX_SOURCE_MAC_PREFIX {0x1a, 0x00, 0x00, 0x00, 0x00}
#define LINK_MAX_LINKS 3

#ifndef CSI_LINK_ID
#define CSI_LINK_ID 0
#endif

/*
 * LINK_TX_SOURCE_MAC: address the transmitter sends from. Bit 0 of the first
 *     byte must be 0 (unicast); 0x1a is also locally administered.
 * LINK_RECEIVER_MAC: base MAC of the link's receiver board, the ESP-NOW
 *     unicast destination. It is board specific, so it is left undefined
 *     until the board is known; the transmitter build then stops with an
 *     error (tx_config.h).
 * LINK_WIFI_CHANNEL: 5 GHz primary channel. All links share channel 36 until
 *     packet loss between links has been measured.
 */

/*
 * Channel width of every csi-tx / csi-rx link, in MHz: 20 (HT20, no
 * secondary channel) or 40 (HT40, secondary channel above).
 *
 * 20 since 2026-10-06 (report 0014): the PC breathing pipeline takes ARI2
 * packets of 57 HT20 subcarriers. All sessions recorded before that, and the
 * onboard occupancy core's subcarrier set, are HT40 (117 pairs); set 40 to
 * rebuild that radio setup. csi-main does not read this value and stays HT40.
 */
#ifndef LINK_WIFI_BANDWIDTH_MHZ
#define LINK_WIFI_BANDWIDTH_MHZ 20
#endif

#if LINK_WIFI_BANDWIDTH_MHZ != 20 && LINK_WIFI_BANDWIDTH_MHZ != 40
#error "LINK_WIFI_BANDWIDTH_MHZ must be 20 or 40"
#endif
#if CSI_LINK_ID == 0

#define LINK_TX_SOURCE_MAC {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00}
#define LINK_RECEIVER_MAC {0x10, 0xbd, 0xa3, 0xd6, 0x5f, 0x40}
#define LINK_WIFI_CHANNEL 36

#elif CSI_LINK_ID == 1

#define LINK_TX_SOURCE_MAC {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01}
#define LINK_RECEIVER_MAC {0x10, 0xbd, 0xa3, 0xd6, 0x8b, 0x84}
#define LINK_WIFI_CHANNEL 36

#elif CSI_LINK_ID == 2

#define LINK_TX_SOURCE_MAC {0x1a, 0x00, 0x00, 0x00, 0x00, 0x02}
#define LINK_WIFI_CHANNEL 36

#else
#error "unknown CSI_LINK_ID: add the link to link_config.h"
#endif
