/* SHA-256 (FIPS 180-4), small and portable: the OTA image check runs the
 * same code on the board and in the host tests. */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
	uint32_t h[8];
	uint64_t len;
	uint8_t buf[64];
	size_t n;
} rr_sha256_t;

void rr_sha256_init(rr_sha256_t *s);
void rr_sha256_update(rr_sha256_t *s, const uint8_t *d, size_t n);
void rr_sha256_final(rr_sha256_t *s, uint8_t out[32]);
