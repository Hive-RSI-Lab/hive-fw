/*
 * Firmware updates the app starts (Settings > Updates): a check against an
 * update manifest, an install from a URL, and an install from a file the
 * phone uploads. The relay's signed, chunked update (hifi_ota.h) is the
 * Hive's path; these are the person's, on their own network, which is no
 * more open than squeezelite's own update page (docs/firmware.md).
 *
 * The manifest is a small JSON file next to the images (docs/test-api.md):
 *   {"latest": "0.2.0", "notes": "...",
 *    "images": {"hifi-plus": {"url": "https://.../hifi-plus-0.2.0.bin",
 *                             "sha256": "<64 hex>"}, ...}}
 * One image for every model may sit at the top level instead ("url",
 * "sha256"). A relative url is taken from the manifest's own folder.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

typedef enum {
	HIFI_UPD_IDLE = 0,
	HIFI_UPD_DOWNLOADING, /* bytes coming in (a URL or an upload) */
	HIFI_UPD_FLASHING,    /* all in: checking the image and switching slots */
	HIFI_UPD_DONE,        /* the next boot runs it */
	HIFI_UPD_ERROR,
} hifi_upd_state_t;

typedef struct {
	char latest[32];
	char url[192];
	char sha256[65]; /* empty: the image's own appended hash is all there is */
	char notes[160];
} hifi_update_offer_t;

typedef struct {
	hifi_upd_state_t state;
	int progress; /* 0-100 */
	char error[96];
} hifi_update_status_t;

const char *hifi_update_state_name(hifi_upd_state_t s);

/*
 * Reads a manifest for `model`, fetched from `manifest_url`. Returns 0, or
 * -1 with *err set (the manifest doesn't parse, or has no image for this
 * model).
 */
int hifi_update_parse(const char *manifest, const char *manifest_url, const char *model,
		      hifi_update_offer_t *out, const char **err);
