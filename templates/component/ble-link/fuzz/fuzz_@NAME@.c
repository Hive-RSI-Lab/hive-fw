// fw-src: @DIR@/@NAME@.c
// fw-src: @DIR@/@NAME@_bleutil.c
// fw-inc: @DIR@/include
/* @NAME@'s receive side under `fw fuzz`: every value a nearby board writes
 * or notifies (the link has no pairing, and the echo's only check on its
 * peer is a BLE address, which is easy to forge) is taken into the queue
 * item by @NAME@_take, as post() in @NAME@_ble.c does (too long: length 0),
 * and goes through decode and on to the sink's counts or the pinger's
 * round trips through @NAME@_receive, the call handle() makes. Byte 0
 * picks the role (echo or ping), the clock's start and, with bit 2, the
 * advertisement instead: the mark walk on raw bytes, the advertisement a
 * board builds, and the payload an MTU allows. Then frames, each after a
 * gap of up to a second and a bit, so sessions go idle and the drift's
 * one-second windows close. A frame is raw bytes or built with
 * @NAME@_encode from fields in the input, so the fuzzer gets past the
 * header and payload checks into the counting. */
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "@NAME@_bleutil.h"

/* Checks an invariant: a failure is reported as a crash, with the input. */
#define FUZZ_ASSERT(c) do { if (!(c)) { fprintf(stderr, "FUZZ_ASSERT %s:%d %s\n", \
    __FILE__, __LINE__, #c); abort(); } } while (0)

#define SESSION 0x5e55107u

typedef struct {
	const uint8_t *d;
	size_t n, at;
} rd_t;

static uint8_t u8(rd_t *z) { return z->at < z->n ? z->d[z->at++] : 0; }
static uint32_t u32(rd_t *z) {
	uint32_t v = 0;
	for (int i = 0; i < 4; i++) v |= (uint32_t) u8(z) << (8 * i);
	return v;
}
static int64_t i64(rd_t *z) {
	uint64_t v = 0;
	for (int i = 0; i < 8; i++) v |= (uint64_t) u8(z) << (8 * i);
	return (int64_t) v;
}

/* One frame from the input into buf: its length (it may be 0, or longer than
 * the radio carries). Times near now_us when the input asks, raw otherwise. */
static size_t frame(rd_t *z, uint8_t *buf, size_t cap, int64_t now_us, uint32_t rx_session) {
	uint8_t k = u8(z);
	size_t n;
	if (k & 1) { /* built */
		@NAME@_frame_t f = {0};
		f.type = (uint8_t) (1 + u8(z) % 3);
		uint8_t s = u8(z);
		f.session = s % 4 == 0 ? SESSION : s % 4 == 1 ? rx_session : s % 4 == 2 ? SESSION + 1 : u32(z);
		f.seq = k & 4 ? u32(z) : u8(z);
		if (k & 2) { /* plausible: a ping sent a little before now, answered in between */
			int64_t back = u32(z) % 3000000, turn = u8(z) * 50;
			int64_t off = (int64_t) (int32_t) u32(z);
			f.t1 = now_us - back;
			f.t2 = f.t1 + back / 2 + off;
			f.t3 = f.t2 + turn;
		} else {
			f.t1 = i64(z);
			f.t2 = i64(z);
			f.t3 = i64(z);
		}
		f.len = (uint16_t) (@UNAME@_HDR + u8(z) % (@UNAME@_MAX - @UNAME@_HDR + 1));
		n = @NAME@_encode(&f, buf, cap);
		FUZZ_ASSERT(n == f.len);
		@NAME@_frame_t back;
		FUZZ_ASSERT(@NAME@_decode(buf, n, &back));
		FUZZ_ASSERT(back.type == f.type && back.session == f.session && back.seq == f.seq && back.t1 == f.t1 &&
		            back.t2 == f.t2 && back.t3 == f.t3 && back.len == f.len);
		if (k & 8) { /* damaged in the air: one byte, or the length */
			uint8_t at = u8(z), x = u8(z);
			if (k & 16)
				n = at % (n + 8);
			else if (x)
				buf[at % n] ^= x;
		}
	} else { /* raw */
		n = u8(z);
		if (k & 2) n |= (size_t) (u8(z) & 1) << 8;
		if (n > cap) n = cap;
		for (size_t i = 0; i < n; i++) buf[i] = u8(z);
	}
	return n;
}

static void json_ok(size_t n, const char *out, size_t cap) {
	FUZZ_ASSERT(n > 0 && n < cap && strlen(out) == n);
	FUZZ_ASSERT(out[0] == '{' && out[n - 1] == '}');
	FUZZ_ASSERT(!strstr(out, "nan") && !strstr(out, "inf"));
}

/* a scanner's view: does this advertisement mark the board with this MAC? */
static void adv(rd_t *z) {
	uint8_t mac[6], other[6], out[@UNAME@_ADV_LEN];
	for (int i = 0; i < 6; i++) mac[i] = other[i] = u8(z);
	other[u8(z) % 6] ^= (uint8_t) (1 + u8(z) % 255);
	uint16_t mtu = (uint16_t) (u8(z) | u8(z) << 8);
	uint16_t p = @NAME@_payload_for_mtu(mtu);
	FUZZ_ASSERT(mtu < 23 ? p == 0 : p == (mtu - 3 < @UNAME@_BLE_PAYLOAD ? mtu - 3 : @UNAME@_BLE_PAYLOAD));
	FUZZ_ASSERT(@NAME@_adv_build(mac, out, sizeof(out) - 1) == 0);
	FUZZ_ASSERT(@NAME@_adv_build(mac, out, sizeof(out)) == @UNAME@_ADV_LEN);
	FUZZ_ASSERT(@NAME@_adv_has_mark(out, sizeof(out), mac) && !@NAME@_adv_has_mark(out, sizeof(out), other));
	/* any bytes at all (a legacy advertisement holds 31, a scan response 31
	 * more): the walk never reads past n */
	size_t n = z->n - z->at;
	uint8_t *raw = malloc(n ? n : 1);
	FUZZ_ASSERT(raw);
	memcpy(raw, z->d + z->at, n);
	(void) @NAME@_adv_has_mark(raw, n, mac);
	free(raw);
}

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t size) {
	if (size < 1) return 0;
	rd_t z = {d, size, 1};
	if (d[0] & 4) {
		adv(&z);
		return 0;
	}
	bool echo = !(d[0] & 1);
	int64_t now = d[0] & 2 ? 0 : (int64_t) 86400 * 1000000 * 30; /* just booted, or up a month */

	static @NAME@_rx_t rx;
	static @NAME@_tx_t tx;
	@NAME@_rx_init(&rx);
	@NAME@_tx_init(&tx, SESSION);
	uint32_t bad = 0, pings = 0, pongs_in = 0;
	uint8_t item[@UNAME@_BLE_PAYLOAD]; /* rx_item_t's data */
	uint8_t air[512] = {0}; /* a damaged length may run past the frame */
	for (int frames = 0; z.at < z.n && frames < 256; frames++) {
		now += (int64_t) (u8(&z) % 40) * 30000;
		if (!echo && u8(&z) & 1) @NAME@_tx_sent(&tx, @UNAME@_HDR, true, now); /* a ping of ours went out */
		size_t len = frame(&z, air, sizeof(air), now, rx.session);
		/* post(): a write or notification of len bytes (0 is possible) */
		size_t took = @NAME@_take(item, sizeof(item), air, len);
		FUZZ_ASSERT(took == (len <= sizeof(item) ? len : 0));
		len = took;
		/* handle(): the same call the radio task makes */
		@NAME@_frame_t pong;
		@NAME@_got_t got = @NAME@_receive(&rx, &tx, echo, !echo, item, len, now, &pong);
		if (got == @UNAME@_GOT_BAD)
			bad++;
		else {
			/* a frame that decodes is exactly what encode makes of it */
			@NAME@_frame_t f;
			uint8_t again[@UNAME@_MAX];
			FUZZ_ASSERT(@NAME@_decode(item, len, &f));
			FUZZ_ASSERT(@NAME@_encode(&f, again, sizeof(again)) == len && !memcmp(again, item, len));
			if (got == @UNAME@_GOT_REPLY) {
				FUZZ_ASSERT(echo && f.type == @UNAME@_PING);
				FUZZ_ASSERT(pong.type == @UNAME@_PONG && pong.session == f.session && pong.seq == f.seq &&
				            pong.t1 == f.t1 && pong.t2 == now && pong.len == f.len);
				pong.t3 = now + 1; /* as send_frame stamps it */
				FUZZ_ASSERT(@NAME@_encode(&pong, again, sizeof(again)) == pong.len);
				pings++;
			} else if (got == @UNAME@_GOT_USED) {
				FUZZ_ASSERT(echo ? f.type == @UNAME@_DATA : f.type == @UNAME@_PONG);
				if (!echo) pongs_in++;
			} else
				FUZZ_ASSERT(got == @UNAME@_GOT_OTHER && (echo ? f.type == @UNAME@_PONG : f.type != @UNAME@_PONG));
		}
		/* the counts after every frame */
		FUZZ_ASSERT(rx.bad == bad && rx.pings == pings);
		/* frames counted new in this session: never more than the sequence numbers below the top */
		FUZZ_ASSERT(rx.rx <= @NAME@_rx_expected(&rx));
		FUZZ_ASSERT(tx.pongs + tx.dup + tx.stray + tx.bad == pongs_in);
		if (tx.pongs) {
			FUZZ_ASSERT(tx.rtt_min_us >= 0 && tx.rtt_min_us <= tx.rtt_max_us);
			double p50 = @NAME@_tx_rtt_pct(&tx, 50), p99 = @NAME@_tx_rtt_pct(&tx, 99);
			FUZZ_ASSERT(p50 >= tx.rtt_min_us && p50 <= p99 && p99 <= tx.rtt_max_us);
			double ppm;
			if (@NAME@_tx_drift_ppm(&tx, &ppm)) FUZZ_ASSERT(isfinite(ppm));
		}
		FUZZ_ASSERT(@NAME@_rx_kbps(&rx) >= 0 && @NAME@_tx_kbps(&tx) >= 0);
	}
	/* the result both ends report */
	static char out[1024];
	json_ok(@NAME@_rx_json(&rx, out, sizeof(out)), out, sizeof(out));
	json_ok(@NAME@_tx_json(&tx, out, sizeof(out)), out, sizeof(out));
	return 0;
}
