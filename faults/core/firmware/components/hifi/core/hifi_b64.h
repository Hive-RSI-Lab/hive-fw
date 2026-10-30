/* Base64 (RFC 4648, with padding) for OTA chunks, captures and signatures. */
#pragma once

#include <stddef.h>
#include <stdint.h>

/* Encoded length of n bytes, without the terminating NUL. */
static inline size_t hifi_b64_len(size_t n) { return (n + 2) / 3 * 4; }

/* Writes NUL-terminated base64 into out (cap >= hifi_b64_len(n) + 1);
 * returns its length, or 0 if it doesn't fit. */
size_t hifi_b64_encode(const uint8_t *in, size_t n, char *out, size_t cap);

/* Decodes len characters of base64; returns the byte count, or -1 on a bad
 * character, bad padding or a result over cap. */
long hifi_b64_decode(const char *in, size_t len, uint8_t *out, size_t cap);
