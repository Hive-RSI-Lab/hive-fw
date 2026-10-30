/*
 * The test API (docs/test-api.md) as pure logic: a request (method, path,
 * body) in, a status and a JSON reply out. The board code implements
 * hifi_ops_t; the HTTP server and the serial console both call
 * hifi_api_handle(), and the host tests call it with a mock board.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "hifi_ir.h"
#include "hifi_meta.h"
#include "hifi_capture.h"
#include "hifi_ota.h"
#include "hifi_pd.h"
#include "hifi_power.h"
#include "hifi_tone.h"
#include "hifi_update.h"

#define HIFI_API_VERSION 1

#define HIFI_EQ_BANDS 10

/* What the tier can do, so one web app serves all three. */
typedef struct {
	bool dj_direct;
	float eq_min_db, eq_max_db;
	int sub_count;
	bool sub_crossover, sub_per_sub;
	int zones;
	bool eth;
	float pdo_v_max;
} hifi_caps_t;

typedef struct {
	const char *model, *board, *fw, *upstream;
	char name[65];     /* the player name people see in their apps */
	char mac[18];
	char hostname[33];
	int flash_mb, psram_mb;
	hifi_caps_t caps;
} hifi_info_t;

typedef enum { HIFI_INPUT_STREAM = 0, HIFI_INPUT_DJ_DIRECT } hifi_input_t;

/* Everything /state reports. Floats that can't be measured are NaN. */
typedef struct {
	uint32_t uptime_s;
	/* power */
	hifi_pwr_state_t pwr_state;
	float vbus_v, pvdd_v, vbus_i_a;
	bool pg, pvdd_on;
	hifi_contract_t contract;
	/* amp */
	bool amp_enabled, amp_fault, amp_latched;
	int amp_faults;
	float amp_temp_c;
	/* governor */
	float gov_budget_w, gov_in_w, gov_out_w, gov_gain_db;
	bool gov_limiting;
	uint32_t gov_events;
	/* audio */
	const char *source; /* idle lms airplay spotify bt test */
	hifi_input_t input;
	bool tone_active;
	hifi_tone_params_t tone;
	float level_dbfs;
	bool sub_on;
	float sub_db;
	float eq[HIFI_EQ_BANDS];
	int volume, max_volume;   /* hifi_vol.h */
	bool mute;
	int zones;                /* 2: zone B below is reported */
	bool zone_b_on;           /* zone B plays zone A's program at its volume */
	hifi_meta_t playing;      /* elapsed_ms as of now; state NONE: null */
	/* remote */
	int ir_learning;          /* an action, or -1 */
	bool ir_learned[HIFI_IR_N];
	bool ir_have_last;        /* any code seen since boot */
	uint32_t ir_last_code;
	/* net, sys */
	const char *net_mode; /* "sta" or "ap" */
	bool eth_present, eth_up; /* a board with Ethernet; its link has an address */
	char ssid[33];
	char hostname[33];
	char ip[16];        /* empty when not connected */
	int rssi;           /* 0 when unknown */
	uint32_t heap_free, psram_free;
	hifi_update_status_t update; /* the app's updates; idle: not reported */
} hifi_snapshot_t;

#define HIFI_SELFTEST_MAX 16

typedef struct {
	char name[24];
	bool pass;
	char value[64];
} hifi_check_t;

typedef struct {
	int n;
	hifi_check_t c[HIFI_SELFTEST_MAX];
} hifi_selftest_t;

/* The relay link (docs/remote.md) */
typedef struct {
	bool configured, connected;
	char url[96], user[48], prefix[24];
	char dev[13];
	int keys;                   /* trusted signing keys in this build */
	uint32_t accepted, rejected; /* commands since boot */
} hifi_remote_status_t;

/* A Wi-Fi network a scan found */
typedef struct {
	char ssid[33];
	int rssi;
	bool secure;
} hifi_wifi_ap_t;

#define HIFI_WIFI_SCAN_MAX 20

/* Which image runs, and what the last rollback was */
typedef struct {
	const char *slot;  /* "ota_0", "ota_1" or "recovery" */
	const char *state; /* "valid", "pending_verify" (on trial) or "unknown" */
	char last_from[32], last_reason[96]; /* empty: no rollback recorded */
} hifi_ota_status_t;

typedef struct {
	void (*info)(hifi_info_t *out);
	void (*snapshot)(hifi_snapshot_t *out);
	/* 0 on success, else an HTTP status with *err set */
	int (*tone_start)(const hifi_tone_params_t *p, bool force, const char **err);
	void (*tone_stop)(void);
	void (*amp_enable)(bool on);
	void (*amp_reset)(void);
	int (*set_input)(hifi_input_t in);
	/* on: -1 leaves it; db: NaN leaves it, else set as near as the
	 * hardware can. Returns the level it set. */
	float (*set_sub)(int on, float db);
	int (*set_eq)(const float gains_db[HIFI_EQ_BANDS]);
	int (*set_name)(const char *name);
	void (*reboot)(void);        /* after the reply has gone */
	void (*factory_reset)(void); /* likewise */
	int (*control)(const char *action); /* 0 ok, -1 unknown action */
	/* -1 leaves a field; 0 ok, -1 out of range (hifi_vol_set) */
	int (*set_volume)(int volume, int mute, int max_volume);
	void (*ir_learn)(int action);  /* hifi_ir_action_t */
	void (*ir_forget)(int action); /* -1: all of them */
	void (*selftest)(hifi_selftest_t *out);

	/* Optional: NULL where the build or board can't (the call answers 404,
	 * or 409 for capture). */
	/* fills frames of interleaved stereo; 0, or an HTTP status with *err */
	int (*capture)(hifi_cap_src_t src, int16_t *pcm, int frames, hifi_cap_info_t *info, const char **err);
	int (*set_wifi)(const char *ssid, const char *password); /* 0: joining */
	void (*remote_status)(hifi_remote_status_t *out);
	int (*remote_set)(const char *url, const char *user, const char *password, const char *prefix);
	hifi_ota_t *ota;   /* the update session, with the board's flash writes */
	void (*ota_status)(hifi_ota_status_t *out);
	/* the networks in range: how many it wrote, or -1 while the radio is busy */
	int (*wifi_scan)(hifi_wifi_ap_t *aps, int max);
	int (*wifi_forget)(void); /* back to the receiver's own network; 0 ok */
	/* zone B's amp on or off (Ultra; core has no level of its own for it) */
	void (*set_zone)(bool on);
	/* the update manifest's offer: 0, or an HTTP status with *err set */
	int (*update_check)(hifi_update_offer_t *out, const char **err);
	/* downloads and installs an image in the background (state's "update");
	 * sha256 NULL: the one the last check offered for that url, if any.
	 * 0, or an HTTP status with *err set */
	int (*update_start)(const char *url, const char *sha256, const char **err);
} hifi_ops_t;

/* Returns the HTTP status; the reply (always a JSON object) is in out. */
int hifi_api_handle(const hifi_ops_t *ops, const char *method, const char *path,
		    const char *body, char *out, size_t cap);

/* The reply buffer a request needs: 4 KB, or more for a capture. */
size_t hifi_api_reply_cap(const char *path, const char *body);

/* Shared by /state and the console */
void hifi_api_write_state(const hifi_snapshot_t *s, char *out, size_t cap);

/* Adds a check to a self-test result. */
void hifi_selftest_add(hifi_selftest_t *t, const char *name, bool pass, const char *fmt, ...);

/*
 * The serial console: turns `hifi <args...>` (argv without "hifi") into an
 * API request. Returns false with a usage line in `out` when the args don't
 * parse. body must hold at least 256 bytes.
 */
bool hifi_cli_request(int argc, const char *const *argv, const char **method,
		      const char **path, char *body, size_t body_cap, char *out, size_t cap);
