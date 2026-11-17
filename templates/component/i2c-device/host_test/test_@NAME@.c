// fw-src: @DIR@/@NAME@.c @DIR@/mock/@NAME@_mock.c
// fw-inc: @DIR@/include @DIR@/mock
/* @NAME@ on this machine, against the mock bus: register reads and writes,
 * a NACK, a timeout, SDA held by a chip and the recovery that frees it, SCL
 * held, the read-only identity check, bad arguments. */
#include <string.h>

#include "@NAME@.h"
#include "@NAME@_mock.h"
#include "unity.h"

#define CHIP 0x22

static @NAME@_mock_t M;
static @NAME@_bus_t B;
static @NAME@_dev_t D;
static @NAME@_mock_chip_t *C;

void setUp(void) {
	@NAME@_mock_init(&M, &B);
	C = @NAME@_mock_add(&M, CHIP);
	for (int i = 0; i < 256; i++) C->reg[i] = (uint8_t) (i ^ 0x5a);
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_init(&D, &B, CHIP, 0));
}
void tearDown(void) {}

static void test_read(void) {
	uint8_t v = 0, buf[4] = {0};
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_probe(&D));
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL_HEX8(0x09 ^ 0x5a, v);
	/* a multi-byte read walks the chip's pointer on, and wraps at 0xff */
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read(&D, 0xfe, buf, 4));
	uint8_t want[4] = {0xfe ^ 0x5a, 0xff ^ 0x5a, 0x00 ^ 0x5a, 0x01 ^ 0x5a};
	TEST_ASSERT_EQUAL_HEX8_ARRAY(want, buf, 4);
	TEST_ASSERT_EQUAL(0, C->writes); /* reading wrote nothing */
	TEST_ASSERT_EQUAL(3, D.n.ok);
}

static void test_write(void) {
	uint8_t data[3] = {1, 2, 3}, v;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_write(&D, 0x40, data, 3));
	TEST_ASSERT_EQUAL_HEX8_ARRAY(data, &C->reg[0x40], 3);
	TEST_ASSERT_EQUAL(3, C->writes);
	/* update writes only the masked bits, and only when they change */
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_write_u8(&D, 0x50, 0xa5));
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_update(&D, 0x50, 0x0f, 0x03));
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0x50, &v));
	TEST_ASSERT_EQUAL_HEX8(0xa3, v);
	uint32_t w = C->writes;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_update(&D, 0x50, 0x0f, 0x03));
	TEST_ASSERT_EQUAL(w, C->writes);
}

static void test_nack(void) {
	uint8_t buf[2] = {0xee, 0xee};
	/* nobody at the address */
	@NAME@_dev_t none;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_init(&none, &B, 0x23, 0));
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_probe(&none));
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_read(&none, 0, buf, 2));
	TEST_ASSERT_EQUAL_HEX8(0xee, buf[0]); /* a failed read leaves the buffer */
	TEST_ASSERT_EQUAL(2, none.n.nack);
	TEST_ASSERT_EQUAL(0, M.resets); /* a NACK is an answer, not a stuck bus: no recovery */
	/* a chip busy for one transaction: retried when asked to */
	M.nack = 1;
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_read(&D, 0, buf, 1));
	D.nack_retries = 2;
	M.nack = 2;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read(&D, 0, buf, 1));
	M.nack = 3;
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_read(&D, 0, buf, 1));
	TEST_ASSERT_EQUAL(0, M.nack);
}

static void test_timeout(void) {
	uint8_t v;
	/* one timeout: recovery, then the second try works */
	M.timeout = 1;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL(1, D.n.timeout);
	TEST_ASSERT_EQUAL(1, D.n.recovered);
	TEST_ASSERT_EQUAL(1, M.resets);
	TEST_ASSERT_EQUAL(2, M.xfers);
	/* two in a row: one recovery, then the timeout is the answer */
	M.timeout = 2;
	TEST_ASSERT_EQUAL(@UNAME@_TIMEOUT, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL(3, D.n.timeout);
	TEST_ASSERT_EQUAL(2, M.resets);
	/* a bus error isn't retried */
	M.buserr = 1;
	TEST_ASSERT_EQUAL(@UNAME@_BUS, @NAME@_read_u8(&D, 0x09, &v));
}

static void test_stuck_sda(void) {
	uint8_t v = 0;
	/* a chip reset mid-read holds SDA for 5 more clocks: recovery clocks
	 * it out, sends a STOP and the read goes through */
	M.sda_stuck = 5;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL_HEX8(0x09 ^ 0x5a, v);
	TEST_ASSERT_EQUAL(5 + 1, M.clocks); /* and the STOP's own clock */
	TEST_ASSERT_EQUAL(1, M.stops);
	TEST_ASSERT_EQUAL(1, D.n.recovered);
	TEST_ASSERT_TRUE(@NAME@_mock_line(&M, @UNAME@_SDA) && @NAME@_mock_line(&M, @UNAME@_SCL));
	/* 9 clocks is the most a chip mid-byte can need */
	M.sda_stuck = 9;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&B));
	/* held for good: 9 clocks, no STOP, STUCK, and the lines back with the controller */
	M.sda_stuck = @UNAME@_MOCK_FOREVER;
	M.clocks = M.stops = 0;
	TEST_ASSERT_EQUAL(@UNAME@_STUCK, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL(@UNAME@_RECOVER_CLOCKS, M.clocks);
	TEST_ASSERT_EQUAL(0, M.stops);
	TEST_ASSERT_EQUAL(1, D.n.stuck);
	TEST_ASSERT_TRUE(M.scl && M.sda);
	/* the chip lets go: the bus works again */
	M.sda_stuck = 0;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0x09, &v));
}

static void test_nack_wait(void) {
	uint8_t v;
	/* a retry waits out a write cycle (an EEPROM's is up to 5 ms) first */
	D.nack_retries = 2;
	M.nack = 2;
	M.waited_us = 0;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&D, 0, &v));
	TEST_ASSERT_EQUAL(2 * 5000, M.waited_us); /* between tries, none after the last */
	/* no retries asked for: no wait */
	D.nack_retries = 0;
	M.nack = 1;
	M.waited_us = 0;
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_read_u8(&D, 0, &v));
	TEST_ASSERT_EQUAL(0, M.waited_us);
}

static void test_timeout_not_owned(void) {
	/* a bus another driver owns (no reset, no lines: install false on the
	 * board): a timeout gets one more try and nothing counts as recovered */
	@NAME@_bus_t b = B;
	b.line_set = NULL;
	b.line_get = NULL;
	b.reset = NULL;
	@NAME@_dev_t d;
	uint8_t v;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_init(&d, &b, CHIP, 0));
	M.timeout = 1;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read_u8(&d, 0x09, &v));
	TEST_ASSERT_EQUAL(0, d.n.recovered);
	TEST_ASSERT_EQUAL(1, d.n.timeout);
	TEST_ASSERT_EQUAL(0, M.resets);
	TEST_ASSERT_EQUAL(0, M.clocks);
	M.timeout = 2;
	TEST_ASSERT_EQUAL(@UNAME@_TIMEOUT, @NAME@_read_u8(&d, 0x09, &v));
	TEST_ASSERT_EQUAL(0, d.n.recovered);
	TEST_ASSERT_EQUAL(0, M.resets);
}

static void test_sda_again(void) {
	/* a chip still mid-read takes SDA again as SCL falls for the STOP:
	 * recovery clocks on inside the same 9 and sends the STOP again */
	M.sda_stuck = 3;
	M.sda_again = 2;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&B));
	TEST_ASSERT_EQUAL(3 + 1 + 1 + 1, M.clocks); /* 3, the first STOP, 1 more, the second STOP */
	TEST_ASSERT_EQUAL(1, M.stops);
	TEST_ASSERT_TRUE(M.scl && M.sda);
	/* one that keeps taking it: STUCK once the clocks run out */
	M.clocks = M.stops = 0;
	M.sda_stuck = 8;
	M.sda_again = 5;
	TEST_ASSERT_EQUAL(@UNAME@_STUCK, @NAME@_recover(&B));
	M.sda_stuck = M.sda_again = 0;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&B));
}

static void test_stuck_scl(void) {
	uint8_t v;
	M.scl_stuck = 1;
	TEST_ASSERT_EQUAL(@UNAME@_STUCK, @NAME@_read_u8(&D, 0x09, &v));
	TEST_ASSERT_EQUAL(0, M.clocks); /* no clocking into a held SCL */
	TEST_ASSERT_EQUAL(1, M.resets);
	M.scl_stuck = 0;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&B));
}

/* a bus that can't hand its lines over: recovery is its reset alone */
static int resets;
static int fail_reset;
static int reset_only(void *ctx) {
	(void) ctx;
	resets++;
	return fail_reset ? @UNAME@_BUS : @UNAME@_OK;
}

static void test_recover_reset_only(void) {
	@NAME@_bus_t b = B;
	b.line_set = NULL;
	b.line_get = NULL;
	b.reset = reset_only;
	resets = fail_reset = 0;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&b));
	TEST_ASSERT_EQUAL(1, resets);
	fail_reset = 1;
	TEST_ASSERT_EQUAL(@UNAME@_BUS, @NAME@_recover(&b));
	b.reset = NULL;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_recover(&b));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_recover(NULL));
	/* a timeout on such a bus whose reset fails is a BUS error */
	@NAME@_dev_t d;
	b.reset = reset_only;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_init(&d, &b, CHIP, 0));
	M.timeout = 1;
	uint8_t v;
	TEST_ASSERT_EQUAL(@UNAME@_BUS, @NAME@_read_u8(&d, 0, &v));
}

static void test_check(void) {
	/* a CH224-like status register: bits 7 reserved 0, a protocol bit set */
	C->reg[0x09] = 0x08;
	const @NAME@_expect_t ok[] = {{0x09, 0x80, 0x00, "status reserved bit"}, {0x09, 0x08, 0x08, "PD"}};
	char why[96];
	uint32_t w = C->writes;
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_check(&D, ok, 2, why, sizeof(why)));
	TEST_ASSERT_EQUAL_STRING("", why);
	const @NAME@_expect_t bad[] = {{0x09, 0x0f, 0x01, "BC"}};
	TEST_ASSERT_EQUAL(@UNAME@_DIFF, @NAME@_check(&D, bad, 1, why, sizeof(why)));
	TEST_ASSERT_EQUAL_STRING("BC (0x09) read 0x08, & 0x0f should be 0x01", why);
	TEST_ASSERT_EQUAL(w, C->writes); /* check only reads */
	M.nack = 1;
	TEST_ASSERT_EQUAL(@UNAME@_NACK, @NAME@_check(&D, ok, 2, why, sizeof(why)));
	TEST_ASSERT_EQUAL_STRING("status reserved bit (0x09): nack", why);
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_check(&D, NULL, 0, NULL, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_check(&D, NULL, 1, why, sizeof(why)));
	/* a short reason buffer is cut, not overrun */
	char tiny[8];
	TEST_ASSERT_EQUAL(@UNAME@_DIFF, @NAME@_check(&D, bad, 1, tiny, sizeof(tiny)));
	TEST_ASSERT_EQUAL(7, strlen(tiny));
}

static void test_args(void) {
	uint8_t buf[@UNAME@_MAX + 1] = {0};
	@NAME@_dev_t d;
	@NAME@_bus_t nox = {0};
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_init(NULL, &B, CHIP, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_init(&d, NULL, CHIP, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_init(&d, &nox, CHIP, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_init(&d, &B, 0x07, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_init(&d, &B, 0x78, 0));
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_init(&d, &B, 0x08, 7));
	TEST_ASSERT_EQUAL(7, d.timeout_ms);
	TEST_ASSERT_EQUAL(50, D.timeout_ms);
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_read(&D, 0, NULL, 1));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_read(&D, 0, buf, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_read(&D, 0, buf, @UNAME@_MAX + 1));
	TEST_ASSERT_EQUAL(@UNAME@_OK, @NAME@_read(&D, 0, buf, @UNAME@_MAX));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_write(&D, 0, NULL, 1));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_write(&D, 0, buf, 0));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_write(&D, 0, buf, @UNAME@_MAX + 1));
	TEST_ASSERT_EQUAL(@UNAME@_ARG, @NAME@_probe(NULL));
	TEST_ASSERT_EQUAL(0, M.xfers - 1); /* only the one good read reached the bus */
	TEST_ASSERT_EQUAL_STRING("stuck", @NAME@_err_name(@UNAME@_STUCK));
	TEST_ASSERT_EQUAL_STRING("?", @NAME@_err_name(99));
}

static void test_counts_json(void) {
	uint8_t v;
	@NAME@_read_u8(&D, 0, &v);
	M.nack = 1;
	@NAME@_read_u8(&D, 0, &v);
	char out[160];
	size_t n = @NAME@_counts_json(&D, out, sizeof(out));
	TEST_ASSERT_EQUAL_STRING("{\"addr\":\"0x22\",\"ok\":1,\"nack\":1,\"timeout\":0,\"recovered\":0,\"stuck\":0}", out);
	TEST_ASSERT_EQUAL(strlen(out), n);
	TEST_ASSERT_EQUAL(0, @NAME@_counts_json(&D, out, 10));
	TEST_ASSERT_EQUAL_STRING("", out);
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_read);
	RUN_TEST(test_write);
	RUN_TEST(test_nack);
	RUN_TEST(test_timeout);
	RUN_TEST(test_stuck_sda);
	RUN_TEST(test_nack_wait);
	RUN_TEST(test_timeout_not_owned);
	RUN_TEST(test_sda_again);
	RUN_TEST(test_stuck_scl);
	RUN_TEST(test_recover_reset_only);
	RUN_TEST(test_check);
	RUN_TEST(test_args);
	RUN_TEST(test_counts_json);
	return UNITY_END();
}
