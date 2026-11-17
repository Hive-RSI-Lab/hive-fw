/*
 * The remote channel on the board: a WebSocket (rr_ws.c) to the Hive's
 * bench endpoint, which carries signed commands in (rr_remote.c) and
 * replies, status and events out. Also the update session's flash writes
 * (esp_ota_*) and the trial boot that keeps or rolls back a new image.
 *
 * The link's settings live in the "settings" NVS partition, namespace
 * "config"; the API's POST remote sets them.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "rr_api.h"
#include "rr_boot.h"

/* Fills what the app knows about its own health for the trial (Wi-Fi,
 * heap, the app's own checks); the relay adds the link's part. */
typedef void (*rr_trial_inputs_fn)(rr_trial_t *t);

/* The update session the API's ota routes use (ops->ota). */
extern rr_ota_t rr_ota_session;

/* Starts the relay task and the command worker. Call once the default NVS,
 * the event loop and Wi-Fi are up. */
void rr_relay_start(const rr_ops_t *ops, rr_trial_inputs_fn inputs);

/* For rr_ops_t */
void rr_relay_status(rr_remote_status_t *r);
int rr_relay_set(const char *url, const char *user, const char *password, const char *prefix, const char *key);
void rr_relay_ota_status(rr_ota_status_t *o);

/* Whether a link is set, and whether it is up: cheap, from any task. */
void rr_relay_up(bool *configured, bool *up);

/* True while the station has an IP. */
bool rr_net_up(void);

/* Internal RAM free once the board settled (rr_heap_boot_due); 0 until then. */
uint32_t rr_boot_heap(void);
