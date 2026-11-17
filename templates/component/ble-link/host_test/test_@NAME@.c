// fw-src: @DIR@/@NAME@.c @DIR@/@NAME@_bleutil.c
// fw-inc: @DIR@/include
/* @NAME@'s link core on this machine: frames, the duplicate window, the
 * sink's counts, the pinger's round trips, clock offset and drift, JSON;
 * and the BLE parts that need no radio: the advert mark, the peer's BLE
 * address, the connection interval and the frame a link's MTU carries. */
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "@NAME@_bleutil.h"
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
	UNITY_BEGIN();
	RUN_TEST(test_frames);
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
