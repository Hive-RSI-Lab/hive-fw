/* @NAME@: the driver over any bus (plain C, host-tested); see include/@NAME@.h */
#include "@NAME@.h"

#include <stdio.h>
#include <string.h>

#define DEFAULT_TIMEOUT_MS 50
#define HALF_CLOCK_US 5 /* 100 kHz while recovering, slow enough for any chip */
#define NACK_WAIT_US 5000 /* before a NACK's retry: an EEPROM-style write cycle is up to 5 ms */
#define STOP_TRIES 3      /* a chip still mid-read can take SDA again as SCL falls for the STOP */

const char *@NAME@_err_name(int e) {
	switch (e) {
	case @UNAME@_OK: return "ok";
	case @UNAME@_NACK: return "nack";
	case @UNAME@_TIMEOUT: return "timeout";
	case @UNAME@_STUCK: return "stuck";
	case @UNAME@_ARG: return "arg";
	case @UNAME@_BUS: return "bus";
	case @UNAME@_DIFF: return "diff";
	default: return "?";
	}
}

int @NAME@_init(@NAME@_dev_t *d, const @NAME@_bus_t *bus, uint8_t addr, uint32_t timeout_ms) {
	if (!d || !bus || !bus->xfer || addr < @UNAME@_ADDR_LO || addr > @UNAME@_ADDR_HI) return @UNAME@_ARG;
	memset(d, 0, sizeof(*d));
	d->bus = bus;
	d->addr = addr;
	d->timeout_ms = timeout_ms ? timeout_ms : DEFAULT_TIMEOUT_MS;
	return @UNAME@_OK;
}

static void wait(const @NAME@_bus_t *b, uint32_t us) {
	if (b->wait_us) b->wait_us(b->ctx, us);
}

static int reset(const @NAME@_bus_t *b) {
	if (!b->reset) return @UNAME@_OK;
	return b->reset(b->ctx) == @UNAME@_OK ? @UNAME@_OK : @UNAME@_BUS;
}

int @NAME@_recover(const @NAME@_bus_t *b) {
	if (!b) return @UNAME@_ARG;
	if (!b->line_set || !b->line_get) return reset(b);
	b->line_set(b->ctx, @UNAME@_SDA, 1);
	b->line_set(b->ctx, @UNAME@_SCL, 1);
	wait(b, HALF_CLOCK_US);
	int stuck = 0, clocks = 0;
	if (!b->line_get(b->ctx, @UNAME@_SCL)) stuck = 1; /* SCL held or shorted: clocking can't help */
	for (int t = 0; !stuck && t < STOP_TRIES; t++) {
		/* a chip stopped mid-read holds SDA low for its next 0 bit: clock it
		 * out, a bit at a time, until it lets go (9 clocks in all) */
		for (; clocks < @UNAME@_RECOVER_CLOCKS && !b->line_get(b->ctx, @UNAME@_SDA); clocks++) {
			b->line_set(b->ctx, @UNAME@_SCL, 0);
			wait(b, HALF_CLOCK_US);
			b->line_set(b->ctx, @UNAME@_SCL, 1);
			wait(b, HALF_CLOCK_US);
			if (!b->line_get(b->ctx, @UNAME@_SCL)) {
				stuck = 1;
				break;
			}
		}
		if (stuck || !b->line_get(b->ctx, @UNAME@_SDA)) break;
		/* a STOP (SDA rising while SCL is high) so every chip is idle */
		b->line_set(b->ctx, @UNAME@_SCL, 0);
		wait(b, HALF_CLOCK_US);
		b->line_set(b->ctx, @UNAME@_SDA, 0);
		wait(b, HALF_CLOCK_US);
		b->line_set(b->ctx, @UNAME@_SCL, 1);
		wait(b, HALF_CLOCK_US);
		b->line_set(b->ctx, @UNAME@_SDA, 1);
		wait(b, HALF_CLOCK_US);
		if (b->line_get(b->ctx, @UNAME@_SCL) && b->line_get(b->ctx, @UNAME@_SDA)) break;
		/* SDA low again: the chip took it as SCL fell; clock on and try again */
	}
	if (!b->line_get(b->ctx, @UNAME@_SCL) || !b->line_get(b->ctx, @UNAME@_SDA)) stuck = 1;
	int r = reset(b); /* the lines go back to the controller either way */
	if (stuck) return @UNAME@_STUCK;
	return r;
}

/* One transaction with the driver's policy: a NACK is tried again
 * nack_retries times; a TIMEOUT gets one recovery and one more try. */
static int xfer(@NAME@_dev_t *d, const uint8_t *w, size_t wn, uint8_t *r, size_t rn) {
	const @NAME@_bus_t *b = d->bus;
	unsigned nacks = 0;
	int recovered = 0;
	for (;;) {
		int e = b->xfer(b->ctx, d->addr, w, wn, r, rn, d->timeout_ms);
		switch (e) {
		case @UNAME@_OK:
			d->n.ok++;
			return @UNAME@_OK;
		case @UNAME@_NACK:
			d->n.nack++;
			if (nacks++ < d->nack_retries) {
				wait(b, NACK_WAIT_US);
				continue;
			}
			return @UNAME@_NACK;
		case @UNAME@_TIMEOUT:
			d->n.timeout++;
			if (recovered) return @UNAME@_TIMEOUT;
			recovered = 1;
			if (!b->reset && !b->line_set) continue; /* a bus another driver owns: it resets itself; one more try */
			e = @NAME@_recover(b);
			if (e == @UNAME@_STUCK) {
				d->n.stuck++;
				return @UNAME@_STUCK;
			}
			if (e != @UNAME@_OK) return @UNAME@_BUS;
			d->n.recovered++;
			continue;
		default:
			return @UNAME@_BUS;
		}
	}
}

int @NAME@_probe(@NAME@_dev_t *d) {
	if (!d || !d->bus) return @UNAME@_ARG;
	return xfer(d, NULL, 0, NULL, 0);
}

int @NAME@_read(@NAME@_dev_t *d, uint8_t reg, uint8_t *buf, size_t n) {
	if (!d || !d->bus || !buf || n == 0 || n > @UNAME@_MAX) return @UNAME@_ARG;
	uint8_t tmp[@UNAME@_MAX];
	int e = xfer(d, &reg, 1, tmp, n);
	if (e == @UNAME@_OK) memcpy(buf, tmp, n);
	return e;
}

int @NAME@_read_u8(@NAME@_dev_t *d, uint8_t reg, uint8_t *v) { return @NAME@_read(d, reg, v, 1); }

int @NAME@_write(@NAME@_dev_t *d, uint8_t reg, const uint8_t *buf, size_t n) {
	if (!d || !d->bus || !buf || n == 0 || n > @UNAME@_MAX) return @UNAME@_ARG;
	uint8_t tmp[1 + @UNAME@_MAX];
	tmp[0] = reg;
	memcpy(tmp + 1, buf, n);
	return xfer(d, tmp, 1 + n, NULL, 0);
}

int @NAME@_write_u8(@NAME@_dev_t *d, uint8_t reg, uint8_t v) { return @NAME@_write(d, reg, &v, 1); }

int @NAME@_update(@NAME@_dev_t *d, uint8_t reg, uint8_t mask, uint8_t val) {
	uint8_t v;
	int e = @NAME@_read_u8(d, reg, &v);
	if (e != @UNAME@_OK) return e;
	uint8_t nv = (uint8_t) ((v & ~mask) | (val & mask));
	return nv == v ? @UNAME@_OK : @NAME@_write_u8(d, reg, nv);
}

int @NAME@_check(@NAME@_dev_t *d, const @NAME@_expect_t *e, size_t n, char *why, size_t cap) {
	if (why && cap) why[0] = 0;
	if (!d || (!e && n)) return @UNAME@_ARG;
	for (size_t i = 0; i < n; i++) {
		uint8_t v;
		const char *what = e[i].what ? e[i].what : "register";
		int r = @NAME@_read_u8(d, e[i].reg, &v);
		if (r != @UNAME@_OK) {
			if (why && cap) snprintf(why, cap, "%s (0x%02x): %s", what, e[i].reg, @NAME@_err_name(r));
			return r;
		}
		if ((v & e[i].mask) != (e[i].want & e[i].mask)) {
			if (why && cap)
				snprintf(why, cap, "%s (0x%02x) read 0x%02x, & 0x%02x should be 0x%02x", what, e[i].reg, v,
					 e[i].mask, e[i].want & e[i].mask);
			return @UNAME@_DIFF;
		}
	}
	return @UNAME@_OK;
}

size_t @NAME@_counts_json(const @NAME@_dev_t *d, char *out, size_t cap) {
	if (!out || !cap) return 0;
	out[0] = 0;
	if (!d) return 0;
	int n = snprintf(out, cap,
			 "{\"addr\":\"0x%02x\",\"ok\":%lu,\"nack\":%lu,\"timeout\":%lu,\"recovered\":%lu,\"stuck\":%lu}",
			 d->addr, (unsigned long) d->n.ok, (unsigned long) d->n.nack, (unsigned long) d->n.timeout,
			 (unsigned long) d->n.recovered, (unsigned long) d->n.stuck);
	if (n < 0 || (size_t) n >= cap) {
		out[0] = 0;
		return 0;
	}
	return (size_t) n;
}
