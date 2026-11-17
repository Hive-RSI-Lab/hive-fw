// fw-src: @DIR@/@NAME@.c
// fw-inc: @DIR@/include
/* @NAME@'s link core on this machine: frames, the duplicate window, the
 * tag (forged, replayed and unkeyed frames), the sink's counts, the
 * pinger's round trips, clock offset and drift, JSON. */
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
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
	/* times no echo could have written: t1 after t4, t3 before t2, times
	 * that would overflow the round trip's sum (found by fuzzing), a round
	 * trip over @UNAME@_RTT_MAX_US */
	@NAME@_frame_t odd[] = {
		{@UNAME@_PONG, 9, 510, a + 10, a, a, 64},
		{@UNAME@_PONG, 9, 511, a, a + 5, a, 64},
		{@UNAME@_PONG, 9, 512, a, INT64_MIN + 1, INT64_MAX, 64},
		{@UNAME@_PONG, 9, 513, a, a, a, 64},
	};
	for (int i = 0; i < 3; i++) @NAME@_tx_pong(t, &odd[i], a);
	@NAME@_tx_pong(t, &odd[3], a + @UNAME@_RTT_MAX_US + 1);
	TEST_ASSERT_EQUAL(4, t->bad);
	TEST_ASSERT_EQUAL(1, t->rtt_bins[@UNAME@_RTT_BINS - 1]);
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
	return UNITY_END();
}
