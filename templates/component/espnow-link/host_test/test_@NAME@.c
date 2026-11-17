// fw-src: @DIR@/@NAME@.c
// fw-inc: @DIR@/include
/* @NAME@'s link core on this machine: frames, the duplicate window, the
 * sink's counts, the pinger's round trips, clock offset and drift, JSON. */
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "unity.h"

void setUp(void) {}
void tearDown(void) {}

static void test_frames(void) {
	uint8_t buf[@UNAME@_MAX];
	@NAME@_frame_t f = {@UNAME@_PING, 7, 12345, -5, 1LL << 40, INT64_MAX, 100}, g;
	TEST_ASSERT_EQUAL(100, @NAME@_encode(&f, buf, sizeof(buf)));
	TEST_ASSERT_TRUE(@NAME@_decode(buf, 100, &g));
	TEST_ASSERT_TRUE(g.type == @UNAME@_PING && g.session == 7 && g.seq == 12345);
	TEST_ASSERT_TRUE(g.t1 == -5 && g.t2 == (1LL << 40) && g.t3 == INT64_MAX && g.len == 100);
	/* damage outside the times and session is caught: a flipped seq byte
	 * breaks the payload's pattern, a flipped type byte makes an unknown
	 * type, a flipped length byte a length that isn't the frame's */
	for (int i = 0; i < 100; i += 13) {
		if ((i >= 14 && i < @UNAME@_HDR) || (i >= 6 && i < 10)) continue;
		uint8_t c[@UNAME@_MAX];
		memcpy(c, buf, 100);
		c[i] ^= 0x10;
		TEST_ASSERT_FALSE(@NAME@_decode(c, 100, &g));
	}
	/* every seq byte reaches the pattern */
	for (int i = 10; i < 14; i++) {
		uint8_t c[@UNAME@_MAX];
		memcpy(c, buf, 100);
		c[i] ^= 0x01;
		TEST_ASSERT_FALSE(@NAME@_decode(c, 100, &g));
	}
	TEST_ASSERT_FALSE(@NAME@_decode(buf, @UNAME@_HDR - 1, &g));
	/* a frame cut short (or padded) is caught by its length */
	for (size_t n = @UNAME@_HDR; n < 100; n += 7) TEST_ASSERT_FALSE(@NAME@_decode(buf, n, &g));
	TEST_ASSERT_FALSE(@NAME@_decode(buf, 101, &g));
	TEST_ASSERT_TRUE(@NAME@_decode(buf, 100, &g));
	{
		uint8_t c[@UNAME@_MAX];
		memcpy(c, buf, 100);
		c[4] = 99; /* the length says 99: still caught at 100 bytes */
		TEST_ASSERT_FALSE(@NAME@_decode(c, 100, &g));
	}
	f.len = @UNAME@_HDR;
	TEST_ASSERT_EQUAL(@UNAME@_HDR, @NAME@_encode(&f, buf, sizeof(buf)));
	f.len = @UNAME@_MAX + 1;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&f, buf, sizeof(buf)));
	f.len = 200;
	TEST_ASSERT_EQUAL(0, @NAME@_encode(&f, buf, 150));
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
		TEST_ASSERT_TRUE(@NAME@_encode(&pong, buf, sizeof(buf)) == 64 && @NAME@_decode(buf, 64, &got));
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
	UNITY_BEGIN();
	RUN_TEST(test_frames);
	RUN_TEST(test_seen);
	RUN_TEST(test_receiver);
	RUN_TEST(test_sender);
	RUN_TEST(test_macs);
	return UNITY_END();
}
