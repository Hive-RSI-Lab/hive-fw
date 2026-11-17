/*
 * The board's test API as pure logic: a request (method, path, body) in, a
 * status and a JSON reply out. The board implements rr_ops_t; the serial
 * console (`api <op> ["json"]`) and the remote channel both call
 * rr_api_handle(), and the host tests call it with a mock board.
 *
 * Paths, all under /api/v1/:
 *   GET  info              model, board, firmware, MAC
 *   POST selftest          {"pass":bool,"checks":[{"name","pass","value"}]}
 *   POST reboot
 *   POST wifi              {"ssid","password"}
 *   GET  remote, POST remote {"url":"wss://...","user","password","key"}
 *   GET  ota, POST ota/begin|chunk|finish|abort   a firmware update
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "rr_ota.h"

#define RR_API_VERSION 1

typedef struct {
	const char *model, *board, *fw;
	char mac[18];
	int flash_mb;
} rr_info_t;

#define RR_SELFTEST_MAX 16

typedef struct {
	char name[24];
	bool pass;
	char value[64];
} rr_check_t;

typedef struct {
	int n;
	rr_check_t c[RR_SELFTEST_MAX];
} rr_selftest_t;

/* The relay link to the Hive's bench endpoint */
typedef struct {
	bool configured, connected;
	char url[128], user[48], prefix[24];
	char dev[13];
	bool has_key;               /* the link has its key from the Hive (never shown) */
	int keys;                   /* trusted signing keys in this build */
	uint32_t accepted, rejected; /* commands since boot */
} rr_remote_status_t;

/* Which image runs, and what the last rollback was */
typedef struct {
	const char *slot;  /* "ota_0", "ota_1" or "recovery" */
	const char *state; /* "valid", "pending_verify" (on trial) or "unknown" */
	char last_from[32], last_reason[96]; /* empty: no rollback recorded */
} rr_ota_status_t;

typedef struct {
	void (*info)(rr_info_t *out);
	void (*selftest)(rr_selftest_t *out);
	void (*reboot)(void); /* after the reply has gone */

	/* Optional: NULL where the build can't (the call answers 404). */
	int (*set_wifi)(const char *ssid, const char *password); /* 0: joining */
	void (*remote_status)(rr_remote_status_t *out);
	/* key: the board's own key for the Hive's wss:// endpoint (64 hex) */
	int (*remote_set)(const char *url, const char *user, const char *password, const char *prefix, const char *key);
	rr_ota_t *ota; /* the update session, with the board's flash writes */
	void (*ota_status)(rr_ota_status_t *out);
} rr_ops_t;

/* Returns the HTTP status; the reply (always a JSON object) is in out. */
int rr_api_handle(const rr_ops_t *ops, const char *method, const char *path,
		  const char *body, char *out, size_t cap);

/* The reply buffer a request needs. */
size_t rr_api_reply_cap(const char *path, const char *body);

/* Adds a check to a self-test result. */
void rr_selftest_add(rr_selftest_t *t, const char *name, bool pass, const char *fmt, ...);
