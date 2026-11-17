/*
 * A firmware update pushed in chunks by the Hive's bench over the signed
 * remote channel: begin with the size and SHA-256, chunks in order, finish.
 * The logic is here; the board gives the flash writes (esp_ota_* on the
 * ESP32, a buffer in the host tests). Nothing boots until the whole image is
 * written and its hash matches. The commands that carry it are signed, so
 * the image itself needs no signature of its own.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "rr_sha256.h"

#define RR_OTA_CHUNK_MAX 4096
#define RR_OTA_APP_MAGIC 0xE9 /* the first byte of every ESP32 app image */

typedef bool (*rr_verify_fn)(const uint8_t pub[32], const uint8_t *msg, size_t n, const uint8_t sig[64]);

typedef struct {
	/* opens the slot it isn't running from; *slot names it ("ota_1") */
	int (*open)(void *ctx, size_t size, const char **slot, const char **err);
	int (*write)(void *ctx, const uint8_t *d, size_t n); /* 0 ok */
	int (*commit)(void *ctx, const char **err);          /* make it the boot image; 0 ok */
	void (*abort)(void *ctx);
	/* optional (NULL: one task only, the host tests): the console and the
	 * relay share the session, so the API takes this around each step */
	void (*lock)(void *ctx);
	void (*unlock)(void *ctx);
	size_t slot_size; /* the largest image a slot holds */
	void *ctx;
} rr_ota_io_t;

typedef struct {
	const rr_ota_io_t *io;
	bool active, done;
	bool held;         /* a task runs a whole update (rr_ota_claim): the API waits */
	size_t size, written;
	uint8_t sha[32];
	char version[32];
	const char *slot;
	rr_sha256_t h;
} rr_ota_t;

void rr_ota_init(rr_ota_t *o, const rr_ota_io_t *io);

/* Each returns an HTTP status (200 ok) with *err set otherwise. A begin
 * while one is open drops the old one: the sender restarted. */
int rr_ota_begin(rr_ota_t *o, size_t size, const char *sha_hex, const char *version, const char **err);
/* 409 if off isn't where the image has got to (o->written says where) */
int rr_ota_chunk(rr_ota_t *o, size_t off, const uint8_t *d, size_t n, const char **err);
/* checks the hash and makes the image the next boot; the caller restarts */
int rr_ota_finish(rr_ota_t *o, const char **err);
void rr_ota_abort(rr_ota_t *o);

/* A task that runs a whole update in one go claims the session first (0, or
 * 409 with *err set when it is held or an update is open) and releases it
 * at the end. While it is held the API's ota steps answer 409. */
int rr_ota_claim(rr_ota_t *o, const char **err);
void rr_ota_release(rr_ota_t *o);
void rr_ota_lock(rr_ota_t *o);
void rr_ota_unlock(rr_ota_t *o);
