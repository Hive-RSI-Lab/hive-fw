#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rr_b64.h"
#include "rr_json.h"
#include "rr_remote.h"

static int hexval(char c) {
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

int rr_remote_set_keys(rr_remote_t *r, const char *list) {
	r->nkeys = 0;
	const char *p = list ? list : "";
	while (*p && r->nkeys < RR_REMOTE_KEYS_MAX) {
		size_t n = strcspn(p, ", ");
		bool ok = n == 64;
		uint8_t k[32];
		for (size_t i = 0; ok && i < 32; i++) {
			int hi = hexval(p[2 * i]), lo = hexval(p[2 * i + 1]);
			ok = hi >= 0 && lo >= 0;
			k[i] = (uint8_t) (hi << 4 | lo);
		}
		if (ok) memcpy(r->keys[r->nkeys++], k, 32);
		p += n;
		while (*p == ',' || *p == ' ') p++;
	}
	return r->nkeys;
}

void rr_remote_dev_id(const uint8_t mac[6], char out[13]) {
	snprintf(out, 13, "%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* {"id":N,"body":{"error":"..."},"status":S} */
static int reject(rr_remote_t *r, uint64_t id, int status, const char *why, char *out, size_t cap) {
	r->rejected++;
	snprintf(out, cap, "{\"id\":%llu,\"body\":{\"error\":\"%s\"},\"status\":%d}", (unsigned long long) id, why, status);
	return status;
}

size_t rr_remote_reply_cap(const char *msg, size_t len) {
	/* only the path matters; the envelope's first line is short enough to scan */
	const char *nl = memchr(msg, '\n', len);
	size_t n = nl ? (size_t) (nl - msg) : len;
	char *line = malloc(n + 1);
	if (!line) return 4096;
	memcpy(line, msg, n);
	line[n] = '\0';
	char path[64] = "";
	const char *body = NULL;
	size_t blen = 0;
	hj_get_str(line, "path", path, sizeof(path));
	char *b = NULL;
	if (hj_get_raw(line, "body", &body, &blen) && (b = malloc(blen + 1))) {
		memcpy(b, body, blen);
		b[blen] = '\0';
	}
	size_t cap = rr_api_reply_cap(path, b) + 64;
	free(b);
	free(line);
	return cap;
}

int rr_remote_handle(rr_remote_t *r, const rr_ops_t *ops, const char *msg, size_t len,
		       int64_t now_s, char *out, size_t cap) {
	const char *nl = memchr(msg, '\n', len);
	if (!nl) return reject(r, 0, 401, "unsigned command", out, cap);
	size_t n = (size_t) (nl - msg);
	const char *sig64 = nl + 1;
	size_t slen = len - n - 1;
	while (slen && (sig64[slen - 1] == '\n' || sig64[slen - 1] == '\r')) slen--;

	char *line = malloc(n + 1);
	if (!line) return reject(r, 0, 500, "out of memory", out, cap);
	memcpy(line, msg, n);
	line[n] = '\0';

	double idd = 0;
	/* range-checked before the cast: an unsigned id past 2^64 is undefined */
	uint64_t id = hj_get_num(line, "id", &idd) && idd > 0 && idd < 18446744073709551616.0 ? (uint64_t) idd : 0;
	int st;
	uint8_t sig[64];
	bool good = false;
	if (rr_b64_decode(sig64, slen, sig, sizeof(sig)) != 64) {
		st = reject(r, id, 401, "bad signature", out, cap);
		goto done;
	}
	if (!r->nkeys || !r->verify) {
		st = reject(r, id, 401, "no trusted key in this build", out, cap);
		goto done;
	}
	for (int k = 0; k < r->nkeys && !good; k++) good = r->verify(r->keys[k], (const uint8_t *) line, n, sig);
	if (!good) {
		st = reject(r, id, 401, "bad signature", out, cap);
		goto done;
	}

	/* signed by the Hive: now it is safe to read */
	double v = 0, ts = 0;
	char dev[16] = "", method[8] = "", path[64] = "";
	if (!hj_get_num(line, "v", &v) || v != 1 || !id || !hj_get_str(line, "method", method, sizeof(method)) ||
	    !hj_get_str(line, "path", path, sizeof(path))) {
		st = reject(r, id, 400, "not a v1 command", out, cap);
		goto done;
	}
	if (!hj_get_str(line, "dev", dev, sizeof(dev)) || strcmp(dev, r->dev)) {
		st = reject(r, id, 401, "not for this board", out, cap);
		goto done;
	}
	if (id <= r->last_id) {
		st = reject(r, id, 409, "stale id", out, cap);
		goto done;
	}
	if (now_s > 0 && (!hj_get_num(line, "ts", &ts) || fabs(ts - (double) now_s) > RR_REMOTE_TS_SLACK)) {
		st = reject(r, id, 409, "ts too far from the board's clock", out, cap);
		goto done;
	}
	r->last_id = id; /* taken even if it fails below: a replay of it is still stale */

	const char *braw = NULL;
	size_t blen = 0;
	char *body = NULL;
	if (hj_get_raw(line, "body", &braw, &blen) && strncmp(braw, "null", 4)) {
		if (!(body = malloc(blen + 1))) {
			st = reject(r, id, 500, "out of memory", out, cap);
			goto done;
		}
		memcpy(body, braw, blen);
		body[blen] = '\0';
	}
	int pre = snprintf(out, cap, "{\"id\":%llu,\"body\":", (unsigned long long) id);
	if (pre < 0 || (size_t) pre + 32 >= cap) {
		free(body);
		st = reject(r, id, 500, "reply buffer too small", out, cap);
		goto done;
	}
	r->accepted++; /* counted once, as accepted or as rejected */
	st = rr_api_handle(ops, method, path, body, out + pre, cap - pre - 20);
	size_t used = strlen(out);
	snprintf(out + used, cap - used, ",\"status\":%d}", st);
	free(body);
done:
	free(line);
	return st;
}
