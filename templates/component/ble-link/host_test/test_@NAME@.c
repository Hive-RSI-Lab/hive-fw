// fw-src: @DIR@/@NAME@.c @DIR@/@NAME@_bleutil.c
// fw-inc: @DIR@/include
/* @NAME@'s link core on this machine: frames, the duplicate window, the
 * tag (forged, replayed and unkeyed frames), the sink's counts, the
 * pinger's round trips, clock offset and drift, JSON;
 * and the BLE parts that need no radio: the advert mark, the peer's BLE
 * address, the connection interval and the frame a link's MTU carries. */
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "@NAME@_bleutil.h"
#include "unity.h"

void setUp(void) {}
void tearDown(void) {}

/* a test key; a board's key comes with each run (the Hive's provisioning, or
 * whoever starts the run), never from the code */
static const uint8_t KEY[@UNAME@_KEY] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
static @NAME@_key_t K;

static void test_frames(void) {
	uint8_t buf[@UNAME@_MAX];
	@NAME@_frame_t f = {@UNAME@_PING, 7, 12345, -5, 1LL << 40, INT64_MAX, 100}, g;
	TEST_ASSERT_EQUAL(100, @NAME@_encode(&K, &f, buf, sizeof(buf)));
	TEST_ASSERT_TRUE(@NAME@_decode(&K, buf, 100, &g));
	TEST_ASSERT_TRUE(g.type == @UNAME@_PING && g.session == 7 && g.seq == 12345);
	TEST_ASSERT_TRUE(g.t1 == -5 && g.t2 == (1LL << 40) && g.t3 == INT64_MAX && g.len == 100);
	/* damage anywhere is caught: the header (times and session too) by the
	 * tag, the payload by its pattern, the tag by itself */
	for (int i = 0; i < 100; i++) {
		uint8_t c[@UNAME@_MAX];
		memcpy(c, buf, 100);
		c[i] ^= 0x10;
		TEST_ASSERT_FALSE(@NAME@_decode(&K, c, 100, &g));
	}
	TEST_ASSERT_FALSE(@NAME@_decode(&K, buf, @UNAME@_MIN - 1, &g));
	/* a frame cut short (or padded) is caught by its length */
	for (size_t n = @UNAME@_MIN; n < 100; n += 7) TEST_ASSERT_FALSE(@NAME@_decode(&K, buf, n, &g));
	TEST_ASSERT_FALSE(@NAME@_decode(&K, buf, 101, &g));
	TEST_ASSERT_TRUE(@NAME@_decode(&K, buf, 100, &g));
	TEST_ASSERT_FALSE(@NAME@_decode(NULL, buf, 100, &g));
	TEST_ASSERT_EQUAL(0, @NAME@_encode(NULL, &f, buf, sizeof(buf)));
	f.len = @UNAME@_MIN;
	TEST_ASSERT_EQUAL(@UNAME@_MIN, @NAME@_encode(&K, &f, buf, sizeof(buf)));
	TEST_ASSERT_TRUE(@NAME@_decode(&K, buf, @UNAME@_MIN, &g));
	f.len = @UNAME@_MIN - 1;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&K, &f, buf, sizeof(buf)));
	f.len = @UNAME@_MAX + 1;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&K, &f, buf, sizeof(buf)));
	f.len = 200;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&K, &f, buf, 150));
}

/* the tag is HMAC-SHA256 (RFC 2104) cut to @UNAME@_TAG bytes: one answer
 * worked out with Python's hmac module; and the key parser */
static void test_tag(void) {
	uint8_t hdr[@UNAME@_HDR], t[@UNAME@_TAG];
	static const uint8_t want[8] = {0x1d, 0x52, 0x7f, 0x43, 0xb4, 0xea, 0x00, 0xa2};
	for (int i = 0; i < @UNAME@_HDR; i++) hdr[i] = (uint8_t) (100 + i);
	@NAME@_tag(&K, hdr, t);
	TEST_ASSERT_EQUAL_MEMORY(want, t, sizeof(t));
	@NAME@_tag(&K, hdr, t); /* the key's states are not used up */
	TEST_ASSERT_EQUAL_MEMORY(want, t, sizeof(t));
	uint8_t k[@UNAME@_KEY], z[@UNAME@_KEY] = {0};
	TEST_ASSERT_TRUE(@NAME@_parse_key("000102030405060708090a0B0c0D0e0F", k));
	TEST_ASSERT_EQUAL_MEMORY(KEY, k, sizeof(k));
	memset(k, 0, sizeof(k));
	TEST_ASSERT_FALSE(@NAME@_parse_key("000102030405060708090a0b0c0d0e0", k));   /* 31 */
	TEST_ASSERT_FALSE(@NAME@_parse_key("000102030405060708090a0b0c0d0e0f0", k)); /* 33 */
	TEST_ASSERT_FALSE(@NAME@_parse_key("000102030405060708090a0b0c0d0e0g", k));
	TEST_ASSERT_FALSE(@NAME@_parse_key("", k));
	TEST_ASSERT_FALSE(@NAME@_parse_key(NULL, k));
	TEST_ASSERT_EQUAL_MEMORY(z, k, sizeof(k)); /* untouched by a refusal */
}

/* A stranger in radio range, with any MAC, can't move the counts: frames
 * under another key (a live session, a jump in seq, a PING, a PONG for our
 * ping) are counted bad and change nothing else, and an untagged (version
 * 2) frame is bad too */
static void test_forged(void) {
	@NAME@_key_t other;
	uint8_t ok[@UNAME@_KEY];
	memcpy(ok, KEY, sizeof(ok));
	ok[15] ^= 1;
	@NAME@_key_init(&other, ok);
	@NAME@_rx_t rx;
	@NAME@_tx_t *tx = calloc(1, sizeof(*tx));
	@NAME@_rx_init(&rx);
	@NAME@_tx_init(tx, 5);
	uint8_t buf[@UNAME@_MAX];
	@NAME@_frame_t pong;
	int64_t now = 1000000;
	for (uint32_t q = 0; q < 10; q++) {
		@NAME@_frame_t f = {@UNAME@_DATA, 5, q, 0, 0, 0, 250};
		TEST_ASSERT_EQUAL(250, @NAME@_encode(&K, &f, buf, sizeof(buf)));
		TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, true, false, buf, 250, now += 1000, &pong));
	}
	TEST_ASSERT_TRUE(rx.rx == 10 && @NAME@_rx_expected(&rx) == 10 && rx.bad == 0);
	@NAME@_rx_t before = rx;
	/* the live session, a seq far ahead: would show 10^6 lost */
	@NAME@_frame_t jump = {@UNAME@_DATA, 5, 1000000, 0, 0, 0, 250};
	TEST_ASSERT_EQUAL(250, @NAME@_encode(&other, &jump, buf, sizeof(buf)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD, @NAME@_receive(&K, &rx, tx, true, false, buf, 250, now += 1000, &pong));
	/* the next seq, which would be counted as received */
	@NAME@_frame_t next = {@UNAME@_DATA, 5, 10, 0, 0, 0, 250};
	TEST_ASSERT_EQUAL(250, @NAME@_encode(&other, &next, buf, sizeof(buf)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD, @NAME@_receive(&K, &rx, tx, true, false, buf, 250, now += 1000, &pong));
	/* a genuine frame with its tag cut off and the length mended */
	TEST_ASSERT_EQUAL(250, @NAME@_encode(&K, &next, buf, sizeof(buf)));
	buf[4] = 250 - @UNAME@_TAG;
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD,
	                  @NAME@_receive(&K, &rx, tx, true, false, buf, 250 - @UNAME@_TAG, now += 1000, &pong));
	/* a version 2 frame (no tag) */
	TEST_ASSERT_EQUAL(250, @NAME@_encode(&K, &next, buf, sizeof(buf)));
	buf[2] = 2;
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD, @NAME@_receive(&K, &rx, tx, true, false, buf, 250, now += 1000, &pong));
	/* a PING under the wrong key gets no answer */
	@NAME@_frame_t ping = {@UNAME@_PING, 5, 1, now, 0, 0, 64};
	TEST_ASSERT_EQUAL(64, @NAME@_encode(&other, &ping, buf, sizeof(buf)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD, @NAME@_receive(&K, &rx, tx, true, false, buf, 64, now += 1000, &pong));
	TEST_ASSERT_EQUAL(5, rx.bad);
	rx.bad = before.bad;
	TEST_ASSERT_EQUAL_MEMORY(&before, &rx, sizeof(rx)); /* nothing else moved */
	/* the pinger: a forged PONG adds no round trip */
	@NAME@_tx_sent(tx, 64, true, now);
	@NAME@_frame_t fp = {@UNAME@_PONG, 5, 0, now, now + 10, now + 20, 64};
	TEST_ASSERT_EQUAL(64, @NAME@_encode(&other, &fp, buf, sizeof(buf)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_BAD, @NAME@_receive(&K, &rx, tx, false, true, buf, 64, now + 500, &pong));
	TEST_ASSERT_TRUE(tx->pongs == 0 && tx->bad == 0 && rx.bad == before.bad + 1);
	/* and the same PONG under the run's key is taken: the check above is the tag */
	TEST_ASSERT_EQUAL(64, @NAME@_encode(&K, &fp, buf, sizeof(buf)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, false, true, buf, 64, now + 500, &pong));
	TEST_ASSERT_EQUAL(1, tx->pongs);
	@NAME@_key_wipe(&other);
	free(tx);
}

/* a stranger who sends a genuine frame again as they heard it, tag and all:
 * what the tag can't stop, and what each end makes of it */
static void test_replay(void) {
	@NAME@_rx_t rx;
	@NAME@_tx_t *tx = calloc(1, sizeof(*tx));
	@NAME@_rx_init(&rx);
	@NAME@_tx_init(tx, 7);
	uint8_t heard[@UNAME@_MAX];
	@NAME@_frame_t pong;
	int64_t now = 1000000;
	/* a PING, answered once; its replays get nothing */
	@NAME@_frame_t p = {@UNAME@_PING, 7, 3, now, 0, 0, 60};
	TEST_ASSERT_EQUAL(60, @NAME@_encode(&K, &p, heard, sizeof(heard)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_REPLY, @NAME@_receive(&K, &rx, tx, true, false, heard, 60, now += 1000, &pong));
	for (int i = 0; i < 3; i++)
		TEST_ASSERT_EQUAL(@UNAME@_GOT_OTHER, @NAME@_receive(&K, &rx, tx, true, false, heard, 60, now += 1000, &pong));
	TEST_ASSERT_TRUE(rx.pings == 1 && rx.ping_dup == 3 && rx.bad == 0);
	/* the next PING of the run is answered; a new session starts the memory again */
	p.seq = 4;
	TEST_ASSERT_EQUAL(60, @NAME@_encode(&K, &p, heard, sizeof(heard)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_REPLY, @NAME@_receive(&K, &rx, tx, true, false, heard, 60, now += 1000, &pong));
	p.session = 8;
	p.seq = 3;
	TEST_ASSERT_EQUAL(60, @NAME@_encode(&K, &p, heard, sizeof(heard)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_REPLY, @NAME@_receive(&K, &rx, tx, true, false, heard, 60, now += 1000, &pong));
	TEST_ASSERT_TRUE(rx.pings == 3 && rx.ping_dup == 3);
	char out[512];
	TEST_ASSERT_TRUE(@NAME@_rx_json(&rx, out, sizeof(out)) > 0);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"ping_dup\":3"));
	/* DATA: the replay is a dup, the counts don't move */
	@NAME@_frame_t d = {@UNAME@_DATA, 7, 0, 0, 0, 0, 100};
	for (uint32_t q = 0; q < 5; q++) {
		d.seq = q;
		TEST_ASSERT_EQUAL(100, @NAME@_encode(&K, &d, heard, sizeof(heard)));
		TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, true, false, heard, 100, now += 1000, &pong));
	}
	TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, true, false, heard, 100, now += 1000, &pong));
	TEST_ASSERT_TRUE(rx.rx == 5 && rx.dup == 1 && @NAME@_rx_expected(&rx) == 5);
	TEST_ASSERT_TRUE(rx.pings == 3 && rx.ping_dup == 3); /* the echo's side kept through the DATA session */
	/* a PONG: taken once, its replay a dup at the pinger */
	@NAME@_tx_sent(tx, 60, true, now);
	@NAME@_frame_t q = {@UNAME@_PING, 7, 0, now, 0, 0, 60}, r;
	@NAME@_pong_for(&q, now + 500, now + 600, &r);
	TEST_ASSERT_EQUAL(60, @NAME@_encode(&K, &r, heard, sizeof(heard)));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, false, true, heard, 60, now + 1100, &pong));
	TEST_ASSERT_EQUAL(@UNAME@_GOT_USED, @NAME@_receive(&K, &rx, tx, false, true, heard, 60, now + 2100, &pong));
	TEST_ASSERT_TRUE(tx->pongs == 1 && tx->dup == 1 && tx->bad == 0);
	free(tx);
}

/* a key not set, or wiped: refused everywhere, though its state is all
 * zeros that anyone could compute */
static void test_key_ready(void) {
	@NAME@_key_t z;
	memset(&z, 0, sizeof(z));
	uint8_t b[@UNAME@_MAX];
	@NAME@_frame_t f = {@UNAME@_DATA, 1, 1, 0, 0, 0, 80}, g;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&z, &f, b, sizeof(b)));
	TEST_ASSERT_EQUAL(80, @NAME@_encode(&K, &f, b, sizeof(b)));
	TEST_ASSERT_FALSE(@NAME@_decode(&z, b, 80, &g));
	/* a frame tagged under the zero state by hand: still refused */
	z.ready = true;
	TEST_ASSERT_EQUAL(80, @NAME@_encode(&z, &f, b, sizeof(b)));
	z.ready = false;
	TEST_ASSERT_FALSE(@NAME@_decode(&z, b, 80, &g));
	@NAME@_key_t k;
	@NAME@_key_init(&k, KEY);
	TEST_ASSERT_TRUE(k.ready);
	TEST_ASSERT_EQUAL(80, @NAME@_encode(&k, &f, b, sizeof(b)));
	@NAME@_key_wipe(&k);
	TEST_ASSERT_FALSE(k.ready);
	TEST_ASSERT_FALSE(@NAME@_decode(&k, b, 80, &g));
	uint8_t key[@UNAME@_KEY] = {1};
	TEST_ASSERT_FALSE(@NAME@_parse_key("0123456789abcdef0123456789abcdeg", key));
	TEST_ASSERT_EQUAL(1, key[0]);
}

static void test_seen(void) {
	@NAME@_seen_t s;
	memset(&s, 0, sizeof(s));
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 0));
	TEST_ASSERT_EQUAL(1, @NAME@_seen(&s, 0));
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 5));
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 3)); /* late, but new */
	TEST_ASSERT_EQUAL(1, @NAME@_seen(&s, 3));
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 5 + @UNAME@_WINDOW));
	TEST_ASSERT_EQUAL(-1, @NAME@_seen(&s, 5)); /* fell out of the window */
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 6));  /* just inside: its old bit was cleared as the window moved */
	memset(&s, 0, sizeof(s));
	for (uint32_t q = 0; q < 5000; q += 2) TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, q));
	TEST_ASSERT_EQUAL(0, @NAME@_seen(&s, 4997)); /* a reused slot isn't a duplicate */
	TEST_ASSERT_EQUAL(1, @NAME@_seen(&s, 4998));
	/* the last value would wrap top to 0 and reset "expected" (found by fuzzing) */
	TEST_ASSERT_EQUAL(-1, @NAME@_seen(&s, UINT32_MAX));
	TEST_ASSERT_EQUAL(4999, s.top);
}

static void test_receiver(void) {
	@NAME@_rx_t r;
	@NAME@_rx_init(&r);
	@NAME@_frame_t f = {@UNAME@_DATA, 1, 0, 0, 0, 0, 250};
	/* 0..99 with 10 and 20 lost, 30 duplicated, 41 before 40 */
	int64_t now = 1000000;
	for (uint32_t q = 0; q < 100; q++) {
		if (q == 10 || q == 20) continue;
		if (q == 40) {
			f.seq = 41;
			@NAME@_rx_data(&r, &f, now += 1000);
			f.seq = 40;
			@NAME@_rx_data(&r, &f, now += 1000);
			q = 41;
			continue;
		}
		f.seq = q;
		@NAME@_rx_data(&r, &f, now += 1000);
		if (q == 30) @NAME@_rx_data(&r, &f, now += 1000);
	}
	@NAME@_rx_bad(&r);
	TEST_ASSERT_EQUAL(98, r.rx);
	TEST_ASSERT_EQUAL(100, @NAME@_rx_expected(&r));
	TEST_ASSERT_TRUE(r.dup == 1 && r.reorder == 1 && r.bad == 1);
	TEST_ASSERT_DOUBLE_WITHIN(0.1, 98 * 250 * 8 / 98.0, @NAME@_rx_kbps(&r));
	char out[512];
	TEST_ASSERT_TRUE(@NAME@_rx_json(&r, out, sizeof(out)) > 0);
	TEST_ASSERT_EQUAL_CHAR('{', out[0]);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"lost\":2,"));
	TEST_ASSERT_NOT_NULL(strstr(out, "\"loss_pct\":2.000"));
	TEST_ASSERT_EQUAL(0, @NAME@_rx_json(&r, out, 20)); /* too small: nothing, not half */
	TEST_ASSERT_EQUAL_STRING("", out);
	/* a frame of another session while this one is live is a stray (a late
	 * frame of the last run must not wipe this run's counts) */
	f.session = 2;
	f.seq = 0;
	@NAME@_rx_data(&r, &f, now + 1000);
	TEST_ASSERT_TRUE(r.rx == 98 && r.session == 1 && r.stray == 1);
	TEST_ASSERT_TRUE(@NAME@_rx_json(&r, out, sizeof(out)) > 0);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"stray\":1,"));
	/* once this one has been silent a second, a new session starts afresh,
	 * keeping the damaged and stray counts */
	@NAME@_rx_data(&r, &f, now + @UNAME@_SESSION_IDLE_US);
	TEST_ASSERT_TRUE(r.rx == 1 && r.session == 2 && r.dup == 0 && r.bad == 1 && r.stray == 1);
	f.session = 1; /* and the old one is now the stray */
	@NAME@_rx_data(&r, &f, now + @UNAME@_SESSION_IDLE_US + 10);
	TEST_ASSERT_TRUE(r.rx == 1 && r.session == 2 && r.stray == 2);
}

static void test_sender(void) {
	@NAME@_tx_t *t = calloc(1, sizeof(*t));
	@NAME@_tx_init(t, 9);
	double ppm;
	TEST_ASSERT_TRUE(@NAME@_tx_rtt_pct(t, 50) < 0);
	TEST_ASSERT_FALSE(@NAME@_tx_drift_ppm(t, &ppm));
	/* The echo's clock reads ours + 5 s, 20 ppm fast. 10 pings a second for
	 * 20 s; round trip 2 ms, every tenth 10 ms and lopsided (the best-of-
	 * second filter must drop it). Ping 50 is lost, ping 7 duplicated. */
	int64_t a = 1000000;
	for (uint32_t q = 0; q < 200; q++, a += 100000) {
		@NAME@_frame_t ping = {@UNAME@_PING, 9, q, a, 0, 0, 64}, pong, got;
		uint8_t buf[@UNAME@_MAX];
		@NAME@_tx_sent(t, ping.len, true, a);
		if (q == 50) {
			@NAME@_tx_acked(t, false);
			continue;
		}
		@NAME@_tx_acked(t, true);
		int64_t out_us = (q % 10 == 9) ? 9000 : 950, back_us = (q % 10 == 9) ? 900 : 950;
		int64_t t2 = (int64_t) ((a + out_us) * (1 + 20e-6)) + 5000000;
		@NAME@_pong_for(&ping, t2, t2 + 100, &pong);
		TEST_ASSERT_TRUE(@NAME@_encode(&K, &pong, buf, sizeof(buf)) == 64 && @NAME@_decode(&K, buf, 64, &got));
		int64_t t4 = a + out_us + 100 + back_us;
		@NAME@_tx_pong(t, &got, t4);
		if (q == 7) @NAME@_tx_pong(t, &got, t4 + 5);
	}
	@NAME@_frame_t stray = {@UNAME@_PONG, 8, 1, 0, 0, 0, 64};
	@NAME@_tx_pong(t, &stray, a);
	TEST_ASSERT_TRUE(t->sent == 200 && t->pongs == 199 && t->dup == 1 && t->stray == 1);
	TEST_ASSERT_TRUE(t->acked == 199 && t->nacked == 1);
	TEST_ASSERT_EQUAL(1900, t->rtt_min_us);
	TEST_ASSERT_DOUBLE_WITHIN(100, 1900, @NAME@_tx_rtt_pct(t, 50));
	TEST_ASSERT_TRUE(@NAME@_tx_rtt_pct(t, 99) > 9000);
	TEST_ASSERT_TRUE(t->windows >= 18);
	TEST_ASSERT_TRUE(@NAME@_tx_drift_ppm(t, &ppm));
	TEST_ASSERT_DOUBLE_WITHIN(0.5, 20.0, ppm);
	/* the last second is still open: the fit counts it */
	TEST_ASSERT_TRUE(t->win_any);
	{
		/* 2.5 s of pings: two windows closed, the third still open, and
		 * the fit has three points */
		@NAME@_tx_t *u = calloc(1, sizeof(*u));
		@NAME@_tx_init(u, 3);
		for (int q = 0; q < 25; q++) {
			int64_t t1 = 1000000 + q * 100000LL, t2 = (int64_t) (t1 * (1 + 50e-6)) + 1000;
			@NAME@_frame_t p = {@UNAME@_PONG, 3, (uint32_t) q, t1, t2, t2 + 10, 64};
			@NAME@_tx_sent(u, 64, true, t1);
			@NAME@_tx_pong(u, &p, t1 + 1010);
		}
		double d;
		TEST_ASSERT_TRUE(u->windows == 2 && u->win_any);
		TEST_ASSERT_TRUE(@NAME@_tx_drift_ppm(u, &d));
		TEST_ASSERT_DOUBLE_WITHIN(1.0, 50.0, d);
		char o[1024];
		TEST_ASSERT_TRUE(@NAME@_tx_json(u, o, sizeof(o)) > 0);
		TEST_ASSERT_NOT_NULL(strstr(o, "\"windows\":3,")); /* the open one too */
		free(u);
	}
	TEST_ASSERT_DOUBLE_WITHIN(30, 5000000 + 20e-6 * 19.5e6, (double) t->offset_us);
	char out[1024];
	TEST_ASSERT_TRUE(@NAME@_tx_json(t, out, sizeof(out)) > 0);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"lost\":1,"));
	TEST_ASSERT_NOT_NULL(strstr(out, "\"drift_ppm\":20."));
	TEST_ASSERT_NOT_NULL(strstr(out, "\"rtt_ms\":{\"min\":1.900,"));
	TEST_ASSERT_EQUAL_CHAR('}', out[strlen(out) - 1]);
	/* a slow pong lands in the last bin, not past the end */
	@NAME@_frame_t slow = {@UNAME@_PONG, 9, 501, a, a, a, 64};
	@NAME@_tx_pong(t, &slow, a + 5000000);
	TEST_ASSERT_EQUAL(1, t->rtt_bins[@UNAME@_RTT_BINS - 1]);
	/* hostile times off the radio: no overflow, counted as bad */
	uint32_t bad0 = t->bad, pongs0 = t->pongs, dup0 = t->dup;
	@NAME@_frame_t h1 = {@UNAME@_PONG, 9, 502, INT64_MIN, 0, 0, 64};
	@NAME@_tx_pong(t, &h1, a);
	@NAME@_frame_t h2 = {@UNAME@_PONG, 9, 503, a, INT64_MIN, INT64_MAX, 64};
	@NAME@_tx_pong(t, &h2, a + 10);
	@NAME@_frame_t h3 = {@UNAME@_PONG, 9, 504, a, INT64_MAX, INT64_MIN, 64};
	@NAME@_tx_pong(t, &h3, a + 10);
	@NAME@_frame_t h4 = {@UNAME@_PONG, 9, 505, a, INT64_MAX - 5, INT64_MAX, 64};
	@NAME@_tx_pong(t, &h4, a + 10);
	TEST_ASSERT_TRUE(t->bad == bad0 + 4 && t->pongs == pongs0);
	/* a round trip over @UNAME@_RTT_MAX_US answers no ping of the run, and a
	 * forged PONG doesn't use up the real one's sequence number */
	@NAME@_frame_t far = {@UNAME@_PONG, 9, 506, a, a, a, 64};
	@NAME@_tx_pong(t, &far, a + @UNAME@_RTT_MAX_US + 1);
	@NAME@_frame_t forged = {@UNAME@_PONG, 9, 507, a + 10, a, a, 64};
	@NAME@_tx_pong(t, &forged, a);
	TEST_ASSERT_TRUE(t->bad == bad0 + 6 && t->pongs == pongs0);
	@NAME@_frame_t real = {@UNAME@_PONG, 9, 507, a, a, a, 64};
	@NAME@_tx_pong(t, &real, a + 100);
	TEST_ASSERT_TRUE(t->pongs == pongs0 + 1 && t->dup == dup0);
	free(t);
}

static void test_macs(void) {
	uint8_t m[6];
	TEST_ASSERT_TRUE(@NAME@_parse_mac("30:76:f5:2d:74:3c", m) && m[0] == 0x30 && m[5] == 0x3c);
	TEST_ASSERT_TRUE(@NAME@_parse_mac("3076F52D742C", m) && m[2] == 0xf5);
	TEST_ASSERT_TRUE(@NAME@_parse_mac("30-76-f5-2d-74-3c", m));
	TEST_ASSERT_FALSE(@NAME@_parse_mac("30:76:f5:2d:74", m));
	TEST_ASSERT_FALSE(@NAME@_parse_mac("30:76:f5:2d:74:3g", m));
	TEST_ASSERT_FALSE(@NAME@_parse_mac(NULL, m));
}

static void test_adv(void) {
	const uint8_t mac[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xe0}, other[6] = {0x30, 0x76, 0xf5, 0x2d, 0x74, 0x2c};
	uint8_t a[31];
	TEST_ASSERT_EQUAL(0, @NAME@_adv_build(mac, a, @UNAME@_ADV_LEN - 1));
	TEST_ASSERT_EQUAL(@UNAME@_ADV_LEN, @NAME@_adv_build(mac, a, sizeof(a)));
	TEST_ASSERT_TRUE(@UNAME@_ADV_LEN <= 31);
	TEST_ASSERT_TRUE(@NAME@_adv_has_mark(a, @UNAME@_ADV_LEN, mac));
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(a, @UNAME@_ADV_LEN, other));
	/* padded with zeros to 31, as a scan response often is */
	memset(a + @UNAME@_ADV_LEN, 0, sizeof(a) - @UNAME@_ADV_LEN);
	TEST_ASSERT_TRUE(@NAME@_adv_has_mark(a, sizeof(a), mac));
	/* cut short: the mark's structure runs past the end */
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(a, @UNAME@_ADV_LEN - 1, mac));
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(a, 0, mac));
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(NULL, 5, mac));
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(a, @UNAME@_ADV_LEN, NULL));
	/* the mark after other manufacturer data and a name */
	uint8_t b[31], *p = b;
	*p++ = 4, *p++ = 0xff, *p++ = 0x4c, *p++ = 0x00, *p++ = 0x02;
	*p++ = 3, *p++ = 0x09, *p++ = 'h', *p++ = 'i';
	size_t n = @NAME@_adv_build(mac, p, sizeof(b) - (size_t) (p - b));
	TEST_ASSERT_EQUAL(@UNAME@_ADV_LEN, n);
	TEST_ASSERT_TRUE(@NAME@_adv_has_mark(b, (size_t) (p - b) + n, mac));
	/* the mark last, ending exactly at the 31-byte limit */
	uint8_t e[31], *q = e;
	size_t pad = sizeof(e) - @UNAME@_ADV_LEN - 2; /* a name structure before it fills the rest */
	*q++ = (uint8_t) (pad + 1), *q++ = 0x09;
	memset(q, 'x', pad), q += pad;
	TEST_ASSERT_EQUAL(@UNAME@_ADV_LEN, @NAME@_adv_build(mac, q, sizeof(e) - (size_t) (q - e)));
	TEST_ASSERT_EQUAL(sizeof(e), (size_t) (q - e) + @UNAME@_ADV_LEN);
	TEST_ASSERT_TRUE(@NAME@_adv_has_mark(e, sizeof(e), mac));
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(e, sizeof(e) - 1, mac));
	/* manufacturer data one byte longer than the mark is not the mark */
	uint8_t c[20];
	memcpy(c, a, @UNAME@_ADV_LEN);
	c[3]++;
	c[@UNAME@_ADV_LEN] = 0;
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(c, @UNAME@_ADV_LEN + 1, mac));
	/* a structure whose length runs past the end ends the walk */
	uint8_t d[] = {30, 0x09, 'x'};
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(d, sizeof(d), mac));
	/* another company's id with the same bytes after it is not the mark */
	memcpy(c, a, @UNAME@_ADV_LEN);
	c[5] = 0x4c;
	TEST_ASSERT_FALSE(@NAME@_adv_has_mark(c, @UNAME@_ADV_LEN, mac));
}

static void test_peer_addr(void) {
	uint8_t out[6];
	/* the lab's boards: station = base, BT = base + 2 */
	const uint8_t self_w[6] = {0x30, 0x76, 0xf5, 0x2d, 0x74, 0x2c}, self_b[6] = {0x30, 0x76, 0xf5, 0x2d, 0x74, 0x2e};
	const uint8_t peer[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xe0};
	@NAME@_peer_ble_addr(peer, self_w, self_b, out);
	const uint8_t want[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xe2};
	TEST_ASSERT_EQUAL_MEMORY(want, out, 6);
	/* a peer ending 0xfe or 0xff: IDF adds to the last byte only, no carry */
	const uint8_t pfe[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xfe}, pff[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xff};
	@NAME@_peer_ble_addr(pfe, self_w, self_b, out);
	const uint8_t wfe[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0x00};
	TEST_ASSERT_EQUAL_MEMORY(wfe, out, 6);
	@NAME@_peer_ble_addr(pff, self_w, self_b, out);
	const uint8_t wff[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0x01};
	TEST_ASSERT_EQUAL_MEMORY(wff, out, 6);
	/* this board itself wrapped (station ...fe, BT ...00): still +2 on byte 5 */
	const uint8_t sw2[6] = {1, 2, 3, 4, 5, 0xfe}, sb2[6] = {1, 2, 3, 4, 5, 0x00};
	@NAME@_peer_ble_addr(peer, sw2, sb2, out);
	TEST_ASSERT_EQUAL_MEMORY(want, out, 6);
	/* addresses that don't share 5 bytes: the 48-bit offset (a custom MAC's
	 * guess; the header says to give that board's address another way) */
	const uint8_t sw3[6] = {0, 0, 0, 0, 0x01, 0xff}, sb3[6] = {0, 0, 0, 0, 0x02, 0x01};
	@NAME@_peer_ble_addr(peer, sw3, sb3, out);
	const uint8_t w3[6] = {0x30, 0x76, 0xf5, 0x2d, 0x73, 0xe2};
	TEST_ASSERT_EQUAL_MEMORY(w3, out, 6);
	const uint8_t top[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff}, sw4[6] = {0, 0, 0, 0, 0, 0}, sb4[6] = {0, 0, 0, 1, 0, 0};
	@NAME@_peer_ble_addr(top, sw4, sb4, out);
	const uint8_t w4[6] = {0, 0, 0, 0, 0xff, 0xff};
	TEST_ASSERT_EQUAL_MEMORY(w4, out, 6);
}

static void test_intervals(void) {
	TEST_ASSERT_EQUAL(6, @NAME@_interval_units(7.5f));
	TEST_ASSERT_EQUAL(12, @NAME@_interval_units(15.0f));
	TEST_ASSERT_EQUAL(24, @NAME@_interval_units(30.0f));
	TEST_ASSERT_EQUAL(80, @NAME@_interval_units(100.0f));
	TEST_ASSERT_EQUAL(8, @NAME@_interval_units(10.0f));
	TEST_ASSERT_EQUAL(9, @NAME@_interval_units(11.0f)); /* 8.8: the nearest */
	TEST_ASSERT_EQUAL(0, @NAME@_interval_units(7.4f));
	TEST_ASSERT_EQUAL(0, @NAME@_interval_units(101.0f));
	TEST_ASSERT_EQUAL(0, @NAME@_interval_units(0.0f));
	TEST_ASSERT_EQUAL(0, @NAME@_interval_units(-15.0f));
	volatile float zero = 0.0f;
	TEST_ASSERT_EQUAL(0, @NAME@_interval_units(zero / zero));
}

static void test_mtu(void) {
	TEST_ASSERT_EQUAL(0, @NAME@_payload_for_mtu(0));
	TEST_ASSERT_EQUAL(0, @NAME@_payload_for_mtu(22));
	TEST_ASSERT_EQUAL(20, @NAME@_payload_for_mtu(23));
	TEST_ASSERT_EQUAL(182, @NAME@_payload_for_mtu(185));
	TEST_ASSERT_EQUAL(@UNAME@_BLE_PAYLOAD, @NAME@_payload_for_mtu(@UNAME@_BLE_MTU));
	TEST_ASSERT_EQUAL(@UNAME@_BLE_PAYLOAD, @NAME@_payload_for_mtu(517));
	TEST_ASSERT_EQUAL(244, @UNAME@_BLE_PAYLOAD);
	/* a full-size frame of the core fits the largest payload */
	TEST_ASSERT_TRUE(@UNAME@_HDR <= @UNAME@_BLE_PAYLOAD);
}

int main(void) {
	@NAME@_key_init(&K, KEY);
	UNITY_BEGIN();
	RUN_TEST(test_frames);
	RUN_TEST(test_tag);
	RUN_TEST(test_forged);
	RUN_TEST(test_replay);
	RUN_TEST(test_key_ready);
	RUN_TEST(test_seen);
	RUN_TEST(test_receiver);
	RUN_TEST(test_sender);
	RUN_TEST(test_macs);
	RUN_TEST(test_adv);
	RUN_TEST(test_peer_addr);
	RUN_TEST(test_intervals);
	RUN_TEST(test_mtu);
	return UNITY_END();
}
