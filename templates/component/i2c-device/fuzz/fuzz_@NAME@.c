// fw-src: @DIR@/@NAME@.c @DIR@/mock/@NAME@_mock.c
// fw-inc: @DIR@/include @DIR@/mock
/* @NAME@ under `fw fuzz`: calls with any length (0-255, past
 * @UNAME@_MAX), any register, any reason-buffer size, on a mock bus whose
 * faults (NACKs, timeouts, bus errors, SDA held for k clocks or for good,
 * SCL held) come from the input. Each op is 4 bytes: what, length,
 * register, faults. After each it checks: canaries around every buffer
 * are intact, a bad length is refused before the bus sees it, a read that
 * fails leaves its buffer as it was and one that works returns the chip's
 * registers, reads and checks never write, the error is a known one, and
 * the controller has its lines back. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "@NAME@_mock.h"

/* Checks an invariant: a failure is reported as a crash, with the input. */
#define FUZZ_ASSERT(c) do { if (!(c)) { fprintf(stderr, "FUZZ_ASSERT %s:%d %s\n", \
    __FILE__, __LINE__, #c); abort(); } } while (0)

#define PAD 16
#define CANARY 0xcc
#define CHIP 0x22

static uint8_t buf[PAD + 256 + PAD];
static char why[PAD + 256 + PAD];

static void canaries(void) {
	for (int i = 0; i < PAD; i++) {
		FUZZ_ASSERT(buf[i] == CANARY && buf[PAD + 256 + i] == CANARY);
		FUZZ_ASSERT((uint8_t) why[i] == CANARY && (uint8_t) why[PAD + 256 + i] == CANARY);
	}
}

static void faults(@NAME@_mock_t *m, uint8_t f) {
	switch (f & 7) {
	case 1: m->nack = (f >> 3) & 3; break;
	case 2: m->timeout = (f >> 3) & 3; break;
	case 3: m->buserr = 1; break;
	case 4: m->sda_stuck = (f >> 3) == 31 ? @UNAME@_MOCK_FOREVER : (f >> 3) & 15; break;
	case 5: m->scl_stuck = 1; break;
	default: m->nack = m->timeout = m->buserr = m->sda_stuck = 0, m->scl_stuck = 0; break;
	}
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
	static @NAME@_mock_t m;
	static @NAME@_bus_t b;
	@NAME@_dev_t d;
	@NAME@_mock_init(&m, &b);
	@NAME@_mock_chip_t *c = @NAME@_mock_add(&m, CHIP);
	for (int i = 0; i < 256; i++) c->reg[i] = (uint8_t) (i * 7 + 3);
	FUZZ_ASSERT(@NAME@_init(&d, &b, size ? data[0] : CHIP, 0) == @UNAME@_OK ||
		    (size && (data[0] < @UNAME@_ADDR_LO || data[0] > @UNAME@_ADDR_HI)));
	if (@NAME@_init(&d, &b, CHIP, 0) != @UNAME@_OK) abort();
	d.nack_retries = size > 1 ? data[1] & 3 : 0;
	memset(buf, CANARY, sizeof(buf));
	memset(why, CANARY, sizeof(why));

	for (size_t at = 2; at + 4 <= size; at += 4) {
		uint8_t op = data[at], n = data[at + 1], reg = data[at + 2];
		faults(&m, data[at + 3]);
		uint8_t before[256], *p = buf + PAD;
		memcpy(before, c->reg, 256);
		uint32_t writes = c->writes;
		unsigned xfers = m.xfers;
		memset(p, (uint8_t) (op ^ n), 256);
		uint8_t was[256];
		memcpy(was, p, 256);
		bool bad = n == 0 || n > @UNAME@_MAX;
		int e;
		switch (op % 6) {
		case 0:
			e = @NAME@_read(&d, reg, p, n);
			FUZZ_ASSERT(c->writes == writes && !memcmp(before, c->reg, 256));
			if (e == @UNAME@_OK)
				for (unsigned i = 0; i < n; i++) FUZZ_ASSERT(p[i] == before[(uint8_t) (reg + i)]);
			else
				FUZZ_ASSERT(!memcmp(was, p, 256));
			FUZZ_ASSERT(!memcmp(was + (bad ? 0 : n), p + (bad ? 0 : n), 256 - (bad ? 0 : n)));
			break;
		case 1:
			e = @NAME@_write(&d, reg, p, n);
			if (e == @UNAME@_OK) {
				FUZZ_ASSERT(c->writes == writes + n);
				for (unsigned i = 0; i < n; i++) FUZZ_ASSERT(c->reg[(uint8_t) (reg + i)] == p[i]);
			}
			break;
		case 2: {
			uint8_t mask = n, val = (uint8_t) ~op;
			e = @NAME@_update(&d, reg, mask, val);
			if (e == @UNAME@_OK) FUZZ_ASSERT((c->reg[reg] & mask) == (val & mask));
			FUZZ_ASSERT(c->writes <= writes + 1);
			bad = false;
			break;
		}
		case 3: {
			/* a table read from the input, and a reason buffer of any size */
			@NAME@_expect_t ex[4];
			size_t k = n & 3;
			for (size_t i = 0; i < k; i++) ex[i] = (@NAME@_expect_t) {(uint8_t) (reg + i), op, n, i & 1 ? NULL : "x"};
			e = @NAME@_check(&d, ex, k, why + PAD, n);
			FUZZ_ASSERT(c->writes == writes && !memcmp(before, c->reg, 256));
			if (n) FUZZ_ASSERT(memchr(why + PAD, 0, n) != NULL);
			bad = false;
			break;
		}
		case 4:
			e = @NAME@_probe(&d);
			FUZZ_ASSERT(c->writes == writes);
			bad = false;
			break;
		default:
			e = @NAME@_recover(&b);
			FUZZ_ASSERT(e == @UNAME@_OK || e == @UNAME@_STUCK);
			if (e == @UNAME@_OK)
				FUZZ_ASSERT(@NAME@_mock_line(&m, @UNAME@_SCL) && @NAME@_mock_line(&m, @UNAME@_SDA));
			bad = false;
			break;
		}
		FUZZ_ASSERT(e >= @UNAME@_OK && e <= @UNAME@_DIFF);
		if (bad) FUZZ_ASSERT(e == @UNAME@_ARG && m.xfers == xfers);
		FUZZ_ASSERT(m.scl && m.sda); /* recovery always hands the lines back */
		canaries();
		char js[128];
		FUZZ_ASSERT(@NAME@_counts_json(&d, js, sizeof(js)) > 0);
	}
	return 0;
}
