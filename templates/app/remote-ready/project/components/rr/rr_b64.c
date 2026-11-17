#include "rr_b64.h"

static const char ALPHA[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t rr_b64_encode(const uint8_t *in, size_t n, char *out, size_t cap) {
	size_t need = rr_b64_len(n);
	if (cap < need + 1) return 0;
	char *o = out;
	for (size_t i = 0; i < n; i += 3) {
		uint32_t v = (uint32_t) in[i] << 16;
		if (i + 1 < n) v |= (uint32_t) in[i + 1] << 8;
		if (i + 2 < n) v |= in[i + 2];
		*o++ = ALPHA[v >> 18 & 63];
		*o++ = ALPHA[v >> 12 & 63];
		*o++ = i + 1 < n ? ALPHA[v >> 6 & 63] : '=';
		*o++ = i + 2 < n ? ALPHA[v & 63] : '=';
	}
	*o = '\0';
	return need;
}

static int val(char c) {
	if (c >= 'A' && c <= 'Z') return c - 'A';
	if (c >= 'a' && c <= 'z') return c - 'a' + 26;
	if (c >= '0' && c <= '9') return c - '0' + 52;
	if (c == '+') return 62;
	if (c == '/') return 63;
	return -1;
}

long rr_b64_decode(const char *in, size_t len, uint8_t *out, size_t cap) {
	if (len % 4) return -1;
	size_t n = 0;
	for (size_t i = 0; i < len; i += 4) {
		int pad = 0, v[4];
		for (int k = 0; k < 4; k++) {
			char c = in[i + k];
			if (c == '=') {
				/* only at the end, only in the last two places */
				if (i + 4 != len || k < 2) return -1;
				pad++;
				v[k] = 0;
			} else {
				if (pad || (v[k] = val(c)) < 0) return -1;
			}
		}
		uint32_t w = (uint32_t) v[0] << 18 | (uint32_t) v[1] << 12 | (uint32_t) v[2] << 6 | (uint32_t) v[3];
		int bytes = 3 - pad;
		if (n + bytes > cap) return -1;
		out[n++] = w >> 16;
		if (bytes > 1) out[n++] = w >> 8 & 0xff;
		if (bytes > 2) out[n++] = w & 0xff;
	}
	return (long) n;
}
