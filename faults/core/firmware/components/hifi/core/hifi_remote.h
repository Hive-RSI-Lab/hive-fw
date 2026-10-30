/*
 * The remote channel's commands (docs/remote.md, "The remote channel"): the
 * board takes a signed command off the relay, checks it, runs it through the
 * test API and builds the reply. The MQTT client and the Ed25519 check are
 * the board's (esp/hifi_relay.c, libsodium); this is the logic between them,
 * tested on a PC.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "hifi_api.h"

#define HIFI_REMOTE_KEYS_MAX 2   /* the Hive's signing keys: one, plus one while rotating */
#define HIFI_REMOTE_TS_SLACK 600 /* seconds a command's ts may be off once the clock is set */

typedef bool (*hifi_verify_fn)(const uint8_t pub[32], const uint8_t *msg, size_t n, const uint8_t sig[64]);

typedef struct {
	char dev[13];       /* the board's id: its Wi-Fi MAC, lower-case hex, no colons */
	uint8_t keys[HIFI_REMOTE_KEYS_MAX][32];
	int nkeys;
	hifi_verify_fn verify;
	uint64_t last_id;   /* the newest command taken; ids only go up */
	uint32_t accepted, rejected;
} hifi_remote_t;

/* Reads a key list "hex64[,hex64]" (the build's HIFI_HIVE_PUBKEY); returns
 * how many were taken. Anything that isn't 64 hex digits is skipped. */
int hifi_remote_set_keys(hifi_remote_t *r, const char *list);

/* Formats a MAC's 6 bytes as the board's id. */
void hifi_remote_dev_id(const uint8_t mac[6], char out[13]);

/*
 * One message from <prefix>/<dev>/cmd (len bytes, not NUL-terminated).
 * Writes the reply {"id":N,"body":{...},"status":S} into out and returns S.
 * now_s is Unix time, or 0 while the board's clock isn't set. A rejected
 * command (401 bad or missing signature, wrong dev; 409 old id or ts too far
 * off; 400 not a command) has no effect.
 */
int hifi_remote_handle(hifi_remote_t *r, const hifi_ops_t *ops, const char *msg, size_t len,
		       int64_t now_s, char *out, size_t cap);

/* The reply buffer a message needs (a capture is large). */
size_t hifi_remote_reply_cap(const char *msg, size_t len);
