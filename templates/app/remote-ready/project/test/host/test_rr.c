// fw-src: components/rr/rr_json.c components/rr/rr_b64.c components/rr/rr_sha256.c components/rr/rr_ws_proto.c
// fw-src: components/rr/rr_api.c components/rr/rr_remote.c components/rr/rr_ota.c components/rr/rr_boot.c
// fw-inc: components/rr/include
/*
 * The remote-ready kit's pure logic on the PC: JSON, base64, SHA-256, the
 * WebSocket framing and the signed command path into the test API, with a
 * mock board and a mock signature check (the board's is libsodium).
 */
#include <stdlib.h>
#include <string.h>

#include "unity.h"
#include "rr_api.h"
#include "rr_b64.h"
#include "rr_boot.h"
#include "rr_json.h"
#include "rr_remote.h"
#include "rr_sha256.h"
#include "rr_ws_proto.h"

static void make_key(void);
void setUp(void) { make_key(); }
void tearDown(void) {}

/* ------------------------------------------------------------------ a mock board */

static int reboots;
static char wifi_ssid[33];

static void m_info(rr_info_t *i) {
	i->model = "demo";
	i->board = "esp32";
	i->fw = "1.0";
	strcpy(i->mac, "aa:bb:cc:dd:ee:ff");
	i->flash_mb = 8;
}
static void m_selftest(rr_selftest_t *t) {
	rr_selftest_add(t, "heap", true, "%d B free", 40000);
	rr_selftest_add(t, "network", false, "no Wi-Fi set");
}
static void m_reboot(void) { reboots++; }
static int m_set_wifi(const char *ssid, const char *pw) {
	snprintf(wifi_ssid, sizeof(wifi_ssid), "%s", ssid);
	return 0;
}

static const rr_ops_t ops = {.info = m_info, .selftest = m_selftest, .reboot = m_reboot, .set_wifi = m_set_wifi};

/* the mock signature: valid when its first byte is the message's length */
static bool m_verify(const uint8_t pub[32], const uint8_t *msg, size_t n, const uint8_t sig[64]) {
	return pub[0] == 0x11 && sig[0] == (uint8_t) n;
}

/* the mock Hive key: "11" then zeros, 64 hex digits, built at run time so
 * no key-shaped constant sits in the template */
static char KEY[65];
static void make_key(void) {
	memset(KEY, '0', 64);
	KEY[0] = KEY[1] = '1';
	KEY[64] = 0;
}

static char *signed_cmd(const char *line, bool good) {
	uint8_t sig[64] = {0};
	sig[0] = good ? (uint8_t) strlen(line) : 0;
	char b64[100];
	size_t n = rr_b64_encode(sig, 64, b64, sizeof(b64));
	char *m = malloc(strlen(line) + n + 2);
	sprintf(m, "%s\n%.*s", line, (int) n, b64);
	return m;
}

static void fresh(rr_remote_t *r) {
	memset(r, 0, sizeof(*r));
	strcpy(r->dev, "aabbccddeeff");
	r->verify = m_verify;
	TEST_ASSERT_EQUAL_INT(1, rr_remote_set_keys(r, KEY));
}

/* ------------------------------------------------------------------ json */

static void test_json_build_and_read(void) {
	char b[128];
	hj_t j;
	hj_init(&j, b, sizeof(b));
	hj_obj(&j, NULL);
	hj_str(&j, "s", "a\"b");
	hj_int(&j, "n", 42);
	hj_bool(&j, "ok", true);
	hj_null(&j, "z");
	hj_obj_end(&j);
	TEST_ASSERT_TRUE(hj_valid_object(b));
	char s[8];
	double n;
	bool ok;
	TEST_ASSERT_TRUE(hj_get_str(b, "s", s, sizeof(s)));
	TEST_ASSERT_EQUAL_STRING("a\"b", s);
	TEST_ASSERT_TRUE(hj_get_num(b, "n", &n));
	TEST_ASSERT_EQUAL_INT(42, (int) n);
	TEST_ASSERT_TRUE(hj_get_bool(b, "ok", &ok) && ok);
	TEST_ASSERT_FALSE(hj_valid_object("{\"a\":"));
}

/* ------------------------------------------------------------------ b64, sha256 */

static void test_b64_round_trip(void) {
	char e[16];
	TEST_ASSERT_EQUAL_UINT(8, rr_b64_encode((const uint8_t *) "hello", 5, e, sizeof(e)));
	TEST_ASSERT_EQUAL_STRING("aGVsbG8=", e);
	uint8_t d[8];
	TEST_ASSERT_EQUAL_INT(5, rr_b64_decode(e, 8, d, sizeof(d)));
	TEST_ASSERT_EQUAL_MEMORY("hello", d, 5);
	TEST_ASSERT_TRUE(rr_b64_decode("a!==", 4, d, sizeof(d)) < 0);
}

static void test_sha256_abc(void) {
	rr_sha256_t s;
	uint8_t h[32];
	rr_sha256_init(&s);
	rr_sha256_update(&s, (const uint8_t *) "abc", 3);
	rr_sha256_final(&s, h);
	static const uint8_t want[4] = {0xba, 0x78, 0x16, 0xbf};
	TEST_ASSERT_EQUAL_MEMORY(want, h, 4);
	TEST_ASSERT_EQUAL_HEX8(0xad, h[31]);
}

/* ------------------------------------------------------------------ the WebSocket protocol */

static void test_ws_url_and_key(void) {
	char host[RR_WS_HOST_MAX], path[RR_WS_PATH_MAX];
	int port;
	TEST_ASSERT_EQUAL_INT(0, rr_ws_url_parse("wss://bench.example.org/board", host, &port, path));
	TEST_ASSERT_EQUAL_STRING("bench.example.org", host);
	TEST_ASSERT_EQUAL_INT(443, port);
	TEST_ASSERT_EQUAL_STRING("/board", path);
	TEST_ASSERT_TRUE(rr_ws_url_parse("ws://plain.example.org/", host, &port, path) != 0);
	TEST_ASSERT_TRUE(rr_ws_key_ok(KEY));
	TEST_ASSERT_FALSE(rr_ws_key_ok("12ab"));
}

static void test_ws_headers(void) {
	char h[RR_WS_HEADERS_MAX];
	TEST_ASSERT_TRUE(rr_ws_headers(h, sizeof(h), "", "", "aabbccddeeff", KEY) > 0);
	TEST_ASSERT_NOT_NULL(strstr(h, "X-Bench-Dev: aabbccddeeff\r\n"));
	TEST_ASSERT_NULL(strstr(h, "CF-Access"));
	TEST_ASSERT_EQUAL_INT(-1, rr_ws_headers(h, sizeof(h), "", "", "aabbccddeeff", ""));
	TEST_ASSERT_EQUAL_INT(-1, rr_ws_headers(h, sizeof(h), "id\r\nX: y", "s", "aabbccddeeff", KEY));
}

static void test_ws_tag_and_head(void) {
	char b[64] = "{\"a\":1}";
	size_t n = rr_ws_tag(b, strlen(b), sizeof(b), "status");
	TEST_ASSERT_TRUE(n > 0);
	TEST_ASSERT_TRUE(hj_valid_object(b));
	char t[16];
	TEST_ASSERT_TRUE(hj_get_str(b, "t", t, sizeof(t)));
	TEST_ASSERT_EQUAL_STRING("status", t);

	uint8_t head[RR_WS_HEAD_MAX], mask[4] = {1, 2, 3, 4};
	size_t hn = rr_ws_head(head, 1, 300, mask);
	TEST_ASSERT_EQUAL_UINT(8, hn); /* 2 + 2 length bytes + 4 mask */
	TEST_ASSERT_EQUAL_HEX8(0x81, head[0]);
	TEST_ASSERT_EQUAL_HEX8(0x80 | 126, head[1]);
	TEST_ASSERT_EQUAL_UINT(4, rr_ws_head_len(head) - 4); /* the mask follows */
}

/* ------------------------------------------------------------------ the test API */

static void test_api_info_and_selftest(void) {
	char out[1024];
	TEST_ASSERT_EQUAL_INT(200, rr_api_handle(&ops, "GET", "/api/v1/info", NULL, out, sizeof(out)));
	char model[16];
	TEST_ASSERT_TRUE(hj_get_str(out, "model", model, sizeof(model)));
	TEST_ASSERT_EQUAL_STRING("demo", model);
	TEST_ASSERT_EQUAL_INT(200, rr_api_handle(&ops, "POST", "/api/v1/selftest", "{}", out, sizeof(out)));
	bool pass = true;
	TEST_ASSERT_TRUE(hj_get_bool(out, "pass", &pass));
	TEST_ASSERT_FALSE(pass);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"network\""));
	TEST_ASSERT_EQUAL_INT(404, rr_api_handle(&ops, "GET", "/api/v1/nothing", NULL, out, sizeof(out)));
	/* no OTA and no relay in the mock: those answer 404 */
	TEST_ASSERT_EQUAL_INT(404, rr_api_handle(&ops, "GET", "/api/v1/ota", NULL, out, sizeof(out)));
	TEST_ASSERT_EQUAL_INT(404, rr_api_handle(&ops, "GET", "/api/v1/remote", NULL, out, sizeof(out)));
}

static void test_api_wifi(void) {
	char out[256];
	TEST_ASSERT_EQUAL_INT(400, rr_api_handle(&ops, "POST", "/api/v1/wifi", "{\"password\":\"x\"}", out, sizeof(out)));
	TEST_ASSERT_EQUAL_INT(200, rr_api_handle(&ops, "POST", "/api/v1/wifi", "{\"ssid\":\"lab\",\"password\":\"x\"}", out, sizeof(out)));
	TEST_ASSERT_EQUAL_STRING("lab", wifi_ssid);
}

/* ------------------------------------------------------------------ signed commands */

static void test_remote_accepts_a_signed_command(void) {
	rr_remote_t r;
	fresh(&r);
	char *m = signed_cmd("{\"v\":1,\"id\":5,\"dev\":\"aabbccddeeff\",\"method\":\"GET\",\"path\":\"/api/v1/info\",\"body\":null}", true);
	size_t cap = rr_remote_reply_cap(m, strlen(m));
	char *out = malloc(cap);
	TEST_ASSERT_EQUAL_INT(200, rr_remote_handle(&r, &ops, m, strlen(m), 0, out, cap));
	TEST_ASSERT_TRUE(hj_valid_object(out));
	double id = 0;
	TEST_ASSERT_TRUE(hj_get_num(out, "id", &id));
	TEST_ASSERT_EQUAL_INT(5, (int) id);
	TEST_ASSERT_NOT_NULL(strstr(out, "\"model\":\"demo\""));
	/* the same id again is a replay */
	TEST_ASSERT_EQUAL_INT(409, rr_remote_handle(&r, &ops, m, strlen(m), 0, out, cap));
	TEST_ASSERT_EQUAL_UINT32(1, r.accepted);
	free(out);
	free(m);
}

static void test_remote_rejects(void) {
	rr_remote_t r;
	fresh(&r);
	char out[512];
	const char *line = "{\"v\":1,\"id\":7,\"dev\":\"aabbccddeeff\",\"method\":\"POST\",\"path\":\"/api/v1/reboot\",\"body\":{}}";
	char *bad = signed_cmd(line, false);
	TEST_ASSERT_EQUAL_INT(401, rr_remote_handle(&r, &ops, bad, strlen(bad), 0, out, sizeof(out)));
	TEST_ASSERT_EQUAL_INT(401, rr_remote_handle(&r, &ops, line, strlen(line), 0, out, sizeof(out)));
	char *other = signed_cmd("{\"v\":1,\"id\":8,\"dev\":\"000000000000\",\"method\":\"GET\",\"path\":\"/api/v1/info\"}", true);
	TEST_ASSERT_EQUAL_INT(401, rr_remote_handle(&r, &ops, other, strlen(other), 0, out, sizeof(out)));
	/* a clock that is set holds ts to it */
	char *old = signed_cmd("{\"v\":1,\"id\":9,\"ts\":1700000000,\"dev\":\"aabbccddeeff\",\"method\":\"GET\",\"path\":\"/api/v1/info\"}", true);
	TEST_ASSERT_EQUAL_INT(409, rr_remote_handle(&r, &ops, old, strlen(old), 1800000000, out, sizeof(out)));
	TEST_ASSERT_EQUAL_INT(0, reboots);
	TEST_ASSERT_EQUAL_UINT32(0, r.accepted);
	free(bad);
	free(other);
	free(old);

	rr_remote_t none;
	memset(&none, 0, sizeof(none));
	strcpy(none.dev, "aabbccddeeff");
	none.verify = m_verify;
	TEST_ASSERT_EQUAL_INT(0, rr_remote_set_keys(&none, "")); /* a build without HIFI_HIVE_PUBKEY */
	char *good = signed_cmd(line, true);
	TEST_ASSERT_EQUAL_INT(401, rr_remote_handle(&none, &ops, good, strlen(good), 0, out, sizeof(out)));
	free(good);
}

/* ------------------------------------------------------------------ the trial */

static void test_trial(void) {
	char why[96];
	rr_trial_t t = {.component_ok = true, .heap_ok = true, .wifi_configured = true, .wifi_ok = false};
	TEST_ASSERT_EQUAL_INT(RR_TRIAL_WAIT, rr_trial_judge(&t, 10, why, sizeof(why)));
	TEST_ASSERT_EQUAL_INT(RR_TRIAL_BAD, rr_trial_judge(&t, RR_TRIAL_LIMIT_S, why, sizeof(why)));
	t.wifi_ok = true;
	TEST_ASSERT_EQUAL_INT(RR_TRIAL_GOOD, rr_trial_judge(&t, 10, why, sizeof(why)));
	t.forced_fail = true;
	TEST_ASSERT_EQUAL_INT(RR_TRIAL_BAD, rr_trial_judge(&t, 1, why, sizeof(why)));
	t.forced_fail = false;
	t.no_key = true; /* a keyless image could never take another update */
	TEST_ASSERT_EQUAL_INT(RR_TRIAL_BAD, rr_trial_judge(&t, 1, why, sizeof(why)));
	TEST_ASSERT_TRUE(rr_heap_line(40000, 30000, why, sizeof(why)));
	TEST_ASSERT_FALSE(rr_heap_line(RR_HEAP_MIN - 1, 0, why, sizeof(why)));
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_json_build_and_read);
	RUN_TEST(test_b64_round_trip);
	RUN_TEST(test_sha256_abc);
	RUN_TEST(test_ws_url_and_key);
	RUN_TEST(test_ws_headers);
	RUN_TEST(test_ws_tag_and_head);
	RUN_TEST(test_api_info_and_selftest);
	RUN_TEST(test_api_wifi);
	RUN_TEST(test_remote_accepts_a_signed_command);
	RUN_TEST(test_remote_rejects);
	RUN_TEST(test_trial);
	return UNITY_END();
}
