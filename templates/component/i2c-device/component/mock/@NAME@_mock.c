/* @NAME@'s bus on the build machine; see @NAME@_mock.h */
#include "@NAME@_mock.h"

#include <string.h>

static @NAME@_mock_chip_t *find(@NAME@_mock_t *m, uint8_t addr) {
	for (int i = 0; i < @UNAME@_MOCK_CHIPS; i++)
		if (m->chip[i].addr && m->chip[i].addr == addr) return &m->chip[i];
	return NULL;
}

int @NAME@_mock_line(const @NAME@_mock_t *m, int line) {
	if (line == @UNAME@_SCL) return m->scl && !m->scl_stuck;
	return m->sda && !m->sda_stuck;
}

static int xfer(void *ctx, uint8_t addr, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, uint32_t timeout_ms) {
	@NAME@_mock_t *m = ctx;
	(void) timeout_ms;
	m->xfers++;
	if ((wn && !w) || (rn && !r)) return @UNAME@_BUS;
	/* a held line, or the controller left with its lines away (recovery
	 * didn't call reset): nothing gets through */
	if (m->scl_stuck || m->sda_stuck || !m->scl || !m->sda) return @UNAME@_TIMEOUT;
	if (m->timeout) {
		m->timeout--;
		return @UNAME@_TIMEOUT;
	}
	if (m->buserr) {
		m->buserr--;
		return @UNAME@_BUS;
	}
	@NAME@_mock_chip_t *c = find(m, addr);
	if (m->nack) {
		m->nack--;
		return @UNAME@_NACK;
	}
	if (!c) return @UNAME@_NACK;
	if (wn) {
		c->ptr = w[0];
		for (size_t i = 1; i < wn; i++) {
			c->reg[c->ptr++] = w[i];
			c->writes++;
		}
	}
	for (size_t i = 0; i < rn; i++) r[i] = c->reg[c->ptr++];
	return @UNAME@_OK;
}

static void line_set(void *ctx, int line, int high) {
	@NAME@_mock_t *m = ctx;
	high = !!high;
	if (line == @UNAME@_SCL) {
		int was = @NAME@_mock_line(m, @UNAME@_SCL);
		m->scl = high;
		if (was && !high && m->sda_again && !m->sda_stuck) {
			m->sda_stuck = m->sda_again; /* a chip still mid-read drives its next 0 bit */
			m->sda_again = 0;
		}
		if (!was && @NAME@_mock_line(m, @UNAME@_SCL)) {
			m->clocks++; /* a chip holding SDA shifts out a bit each clock */
			if (m->sda_stuck && m->sda_stuck != @UNAME@_MOCK_FOREVER) m->sda_stuck--;
		}
	} else {
		int was = @NAME@_mock_line(m, @UNAME@_SDA);
		m->sda = high;
		if (!was && @NAME@_mock_line(m, @UNAME@_SDA) && @NAME@_mock_line(m, @UNAME@_SCL)) m->stops++;
	}
}

static int line_get(void *ctx, int line) { return @NAME@_mock_line(ctx, line); }

static void wait_us(void *ctx, uint32_t us) { ((@NAME@_mock_t *) ctx)->waited_us += us; }

static int reset(void *ctx) {
	@NAME@_mock_t *m = ctx;
	m->resets++;
	m->scl = m->sda = 1; /* the controller has its lines back, let go */
	return @UNAME@_OK;
}

void @NAME@_mock_init(@NAME@_mock_t *m, @NAME@_bus_t *b) {
	memset(m, 0, sizeof(*m));
	m->scl = m->sda = 1;
	b->xfer = xfer;
	b->line_set = line_set;
	b->line_get = line_get;
	b->wait_us = wait_us;
	b->reset = reset;
	b->ctx = m;
}

@NAME@_mock_chip_t *@NAME@_mock_add(@NAME@_mock_t *m, uint8_t addr) {
	for (int i = 0; i < @UNAME@_MOCK_CHIPS; i++)
		if (!m->chip[i].addr) {
			memset(&m->chip[i], 0, sizeof(m->chip[i]));
			m->chip[i].addr = addr;
			return &m->chip[i];
		}
	return NULL;
}
