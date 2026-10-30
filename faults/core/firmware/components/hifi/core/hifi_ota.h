/*
 * A firmware update pushed in chunks (docs/remote.md, "Firmware updates the
 * Hive pushes"): begin with the size and SHA-256, chunks in order, finish.
 * The logic is here; the board gives the flash writes (esp_ota_* on the
 * ESP32, a buffer in the host tests). Nothing boots until the whole image is
 * written and its hash matches.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "hifi_sha256.h"

#define HIFI_OTA_CHUNK_MAX 4096
#define HIFI_OTA_APP_MAGIC 0xE9 /* the first byte of every ESP32 app image */

typedef struct {
	/* opens the slot it isn't running from; *slot names it ("ota_1") */
	int (*open)(void *ctx, size_t size, const char **slot, const char **err);
	int (*write)(void *ctx, const uint8_t *d, size_t n); /* 0 ok */
	int (*commit)(void *ctx, const char **err);          /* make it the boot image; 0 ok */
	void (*abort)(void *ctx);
	size_t slot_size; /* the largest image a slot holds */
	void *ctx;
} hifi_ota_io_t;

typedef struct {
	const hifi_ota_io_t *io;
	bool active, done;
	size_t size, written;
	uint8_t sha[32];
	bool check_sha;    /* false: the image's own appended hash only (commit checks it) */
	char version[32];
	const char *slot;
	hifi_sha256_t h;
} hifi_ota_t;

void hifi_ota_init(hifi_ota_t *o, const hifi_ota_io_t *io);

/* Each returns an HTTP status (200 ok) with *err set otherwise. A begin
 * while one is open drops the old one: the sender restarted. */
int hifi_ota_begin(hifi_ota_t *o, size_t size, const char *sha_hex, const char *version, const char **err);
/* The same for an image from the person's own network (the app's upload,
 * or a URL from a manifest without a hash): no SHA-256 to match, so only
 * the image's own appended hash, which the board's commit checks
 * (esp_ota_end), stands between it and the boot slot. */
int hifi_ota_begin_local(hifi_ota_t *o, size_t size, const char *version, const char **err);
/* 409 if off isn't where the image has got to (o->written says where) */
int hifi_ota_chunk(hifi_ota_t *o, size_t off, const uint8_t *d, size_t n, const char **err);
/* checks the hash and makes the image the next boot; the caller restarts */
int hifi_ota_finish(hifi_ota_t *o, const char **err);
void hifi_ota_abort(hifi_ota_t *o);
