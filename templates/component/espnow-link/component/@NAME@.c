#include "@NAME@.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* ------------------------------------------------- a small JSON writer */

typedef struct {
	char *out;
	size_t cap, n;
	bool first, overflow;
} jw_t;

static void jw_raw(jw_t *j, const char *fmt, ...) {
	if (j->overflow) return;
	va_list ap;
	va_start(ap, fmt);
	int k = vsnprintf(j->out + j->n, j->cap - j->n, fmt, ap);
	va_end(ap);
	if (k < 0 || (size_t) k >= j->cap - j->n)
		j->overflow = true;
	else
		j->n += (size_t) k;
}
static void jw_key(jw_t *j, const char *k) {
	jw_raw(j, "%s\"%s\":", j->first ? "" : ",", k);
	j->first = false;
}
static void jw_int(jw_t *j, const char *k, long long v) {
	jw_key(j, k);
	jw_raw(j, "%lld", v);
}
static void jw_num(jw_t *j, const char *k, double v, int prec) {
	jw_key(j, k);
	jw_raw(j, "%.*f", prec, v);
}
static void jw_null(jw_t *j, const char *k) {
	jw_key(j, k);
	jw_raw(j, "null");
}
static void jw_obj(jw_t *j, const char *k) {
	if (k) jw_key(j, k);
	jw_raw(j, "{");
	j->first = true;
}
static void jw_obj_end(jw_t *j) {
	jw_raw(j, "}");
	j->first = false;
}
static void jw_begin(jw_t *j, char *out, size_t cap) {
	j->out = out;
	j->cap = cap;
	j->n = 0;
	j->overflow = !out || !cap;
	if (!j->overflow) out[0] = 0;
	jw_obj(j, NULL);
}
static size_t jw_end(jw_t *j) {
	jw_obj_end(j);
	if (j->overflow) {
		if (j->out && j->cap) j->out[0] = 0;
		return 0;
	}
	return j->n;
}



#ifndef @UNAME@_MAGIC0 /* two bytes that mark this link's frames; change them per link */
#define @UNAME@_MAGIC0 'H'
#define @UNAME@_MAGIC1 'L'
#endif
#define MAGIC0 @UNAME@_MAGIC0
#define MAGIC1 @UNAME@_MAGIC1
#define VERSION 2

static void put32(uint8_t *p, uint32_t v) {
	for (int i = 0; i < 4; i++) p[i] = (uint8_t) (v >> (8 * i));
}
static void put64(uint8_t *p, int64_t v) {
	uint64_t u = (uint64_t) v;
	for (int i = 0; i < 8; i++) p[i] = (uint8_t) (u >> (8 * i));
}
static uint32_t get32(const uint8_t *p) {
	uint32_t v = 0;
	for (int i = 0; i < 4; i++) v |= (uint32_t) p[i] << (8 * i);
	return v;
}
static int64_t get64(const uint8_t *p) {
	uint64_t v = 0;
	for (int i = 0; i < 8; i++) v |= (uint64_t) p[i] << (8 * i);
	return (int64_t) v;
}

/* the payload byte at offset i of a frame with this sequence number: every
 * byte of the sequence number reaches the pattern (a multiply by an odd
 * constant carries each byte upwards), so a damaged seq is caught too */
static uint8_t pattern(uint32_t seq, size_t i) {
	uint32_t h = seq * 2654435761u;
	return (uint8_t) ((h >> (8 * (i % 4))) ^ (i * 7u) ^ 0x5a);
}

size_t @NAME@_encode(const @NAME@_frame_t *f, uint8_t *buf, size_t cap) {
	size_t n = f->len;
	if (n < @UNAME@_HDR || n > @UNAME@_MAX || n > cap) return 0;
	buf[0] = MAGIC0;
	buf[1] = MAGIC1;
	buf[2] = VERSION;
	buf[3] = f->type;
	buf[4] = (uint8_t) n; /* the frame's own length: a truncated one is caught */
	buf[5] = (uint8_t) (n >> 8);
	put32(buf + 6, f->session);
	put32(buf + 10, f->seq);
	put64(buf + 14, f->t1);
	put64(buf + 22, f->t2);
	put64(buf + 30, f->t3);
	for (size_t i = @UNAME@_HDR; i < n; i++) buf[i] = pattern(f->seq, i);
	return n;
}

bool @NAME@_decode(const uint8_t *buf, size_t n, @NAME@_frame_t *f) {
	if (!buf || n < @UNAME@_HDR || n > @UNAME@_MAX) return false;
	if (buf[0] != MAGIC0 || buf[1] != MAGIC1 || buf[2] != VERSION) return false;
	if (buf[3] < @UNAME@_PING || buf[3] > @UNAME@_DATA) return false;
	if ((size_t) (buf[4] | buf[5] << 8) != n) return false; /* truncated (or padded) */
	uint32_t seq = get32(buf + 10);
	for (size_t i = @UNAME@_HDR; i < n; i++)
		if (buf[i] != pattern(seq, i)) return false;
	f->type = buf[3];
	f->session = get32(buf + 6);
	f->seq = seq;
	f->t1 = get64(buf + 14);
	f->t2 = get64(buf + 22);
	f->t3 = get64(buf + 30);
	f->len = (uint16_t) n;
	return true;
}

/* --------------------------------------------------------------- dup check */

static void bit_clear(@NAME@_seen_t *s, uint32_t seq) {
	uint32_t i = seq % @UNAME@_WINDOW;
	s->bits[i / 64] &= ~(1ull << (i % 64));
}

int @NAME@_seen(@NAME@_seen_t *s, uint32_t seq) {
	uint32_t i = seq % @UNAME@_WINDOW;
	uint64_t mask = 1ull << (i % 64);
	if (seq >= s->top) {
		/* move the window up: forget what falls out of it */
		if ((uint64_t) seq - s->top >= @UNAME@_WINDOW)
			memset(s->bits, 0, sizeof(s->bits));
		else
			for (uint32_t q = s->top; q < seq; q++) bit_clear(s, q);
		bit_clear(s, seq);
		s->bits[i / 64] |= mask;
		s->top = seq + 1;
		return 0;
	}
	if (s->top - seq > @UNAME@_WINDOW) return -1;
	if (s->bits[i / 64] & mask) return 1;
	s->bits[i / 64] |= mask;
	return 0;
}

/* ---------------------------------------------------------- receiving end */

void @NAME@_rx_init(@NAME@_rx_t *r) { memset(r, 0, sizeof(*r)); }

void @NAME@_rx_data(@NAME@_rx_t *r, const @NAME@_frame_t *f, int64_t now_us) {
	if (r->rx && f->session != r->session && now_us - r->last_us < @UNAME@_SESSION_IDLE_US) {
		r->stray++;
		return;
	}
	if (r->rx == 0 || f->session != r->session) {
		uint32_t bad = r->bad, pings = r->pings, stray = r->stray;
		@NAME@_rx_init(r);
		r->bad = bad;
		r->pings = pings;
		r->stray = stray;
		r->session = f->session;
		r->first_us = now_us;
	}
	uint32_t top = r->seen.top;
	int s = @NAME@_seen(&r->seen, f->seq);
	if (s < 0) {
		r->old++;
		return;
	}
	if (s > 0) {
		r->dup++;
		return;
	}
	if (top && f->seq + 1 < top) r->reorder++;
	r->rx++;
	r->bytes += f->len;
	r->last_us = now_us;
}

void @NAME@_rx_bad(@NAME@_rx_t *r) { r->bad++; }

uint32_t @NAME@_rx_expected(const @NAME@_rx_t *r) { return r->seen.top; }

double @NAME@_rx_kbps(const @NAME@_rx_t *r) {
	int64_t us = r->last_us - r->first_us;
	if (r->rx < 2 || us <= 0) return 0;
	return r->bytes * 8.0 * 1000.0 / us; /* bits/us*1000 = kbit/s */
}

size_t @NAME@_rx_json(const @NAME@_rx_t *r, char *out, size_t cap) {
	jw_t jw, *j = &jw;
	jw_begin(j, out, cap);
	uint32_t exp = @NAME@_rx_expected(r);
	jw_int(j, "session", r->session);
	jw_int(j, "rx", r->rx);
	jw_int(j, "expected", exp);
	jw_int(j, "lost", exp > r->rx ? exp - r->rx : 0);
	jw_num(j, "loss_pct", exp ? 100.0 * (exp > r->rx ? exp - r->rx : 0) / exp : 0, 3);
	jw_int(j, "dup", r->dup);
	jw_int(j, "reorder", r->reorder);
	jw_int(j, "old", r->old);
	jw_int(j, "bad", r->bad);
	jw_int(j, "stray", r->stray);
	jw_int(j, "pings", r->pings);
	jw_int(j, "bytes", (long long) r->bytes);
	jw_num(j, "seconds", r->rx > 1 ? (r->last_us - r->first_us) / 1e6 : 0, 3);
	jw_num(j, "kbps", @NAME@_rx_kbps(r), 1);
	return jw_end(j);
}

void @NAME@_pong_for(const @NAME@_frame_t *ping, int64_t t2, int64_t t3, @NAME@_frame_t *pong) {
	*pong = *ping;
	pong->type = @UNAME@_PONG;
	pong->t2 = t2;
	pong->t3 = t3;
}

/* ------------------------------------------------------------ sending end */

void @NAME@_tx_init(@NAME@_tx_t *t, uint32_t session) {
	memset(t, 0, sizeof(*t));
	t->session = session;
	t->rtt_min_us = -1;
}

void @NAME@_tx_sent(@NAME@_tx_t *t, size_t len, bool ok, int64_t now_us) {
	if (!ok) {
		t->send_fail++;
		return;
	}
	if (!t->sent) t->first_us = now_us;
	t->sent++;
	t->bytes += len;
	t->last_us = now_us;
}

void @NAME@_tx_acked(@NAME@_tx_t *t, bool acked) {
	if (acked)
		t->acked++;
	else
		t->nacked++;
}

static void close_window(@NAME@_tx_t *t) {
	if (!t->win_any) return;
	if (!t->windows) {
		t->x0_us = t->win_start_us;
		t->y0_us = t->win_offset_us;
	}
	double x = (t->win_start_us - t->x0_us) / 1e6;
	/* the offset relative to the first window keeps the sums small */
	double y = (double) (t->win_offset_us - t->y0_us);
	t->sx += x;
	t->sy += y;
	t->sxx += x * x;
	t->sxy += x * y;
	t->windows++;
	t->offset_us = t->win_offset_us;
	t->offset_delay_us = t->win_delay_us;
	t->win_any = false;
}

void @NAME@_tx_pong(@NAME@_tx_t *t, const @NAME@_frame_t *f, int64_t t4) {
	if (f->type != @UNAME@_PONG || f->session != t->session) {
		t->stray++;
		return;
	}
	int s = @NAME@_seen(&t->seen, f->seq);
	if (s != 0) {
		t->dup++;
		return;
	}
	int64_t delay = (t4 - f->t1) - (f->t3 - f->t2);
	if (delay < 0 || f->t1 > t4) {
		t->bad++;
		return;
	}
	t->pongs++;
	int64_t bin = delay / @UNAME@_RTT_BIN_US;
	if (bin >= @UNAME@_RTT_BINS) bin = @UNAME@_RTT_BINS - 1;
	t->rtt_bins[bin]++;
	if (t->rtt_min_us < 0 || delay < t->rtt_min_us) t->rtt_min_us = delay;
	if (delay > t->rtt_max_us) t->rtt_max_us = delay;
	t->rtt_sum_us += delay;

	int64_t offset = ((f->t2 - f->t1) + (f->t3 - t4)) / 2;
	if (t->win_any && f->t1 - t->win_start_us >= 1000000) close_window(t);
	if (!t->win_any) {
		t->win_any = true;
		t->win_start_us = f->t1;
		t->win_delay_us = delay;
		t->win_offset_us = offset;
	} else if (delay < t->win_delay_us) {
		t->win_delay_us = delay;
		t->win_offset_us = offset;
	}
	if (!t->windows) { /* until a window closes, report the best so far */
		t->offset_us = t->win_offset_us;
		t->offset_delay_us = t->win_delay_us;
	}
}

double @NAME@_tx_rtt_pct(const @NAME@_tx_t *t, double p) {
	if (!t->pongs) return -1;
	if (p <= 0) return (double) t->rtt_min_us;
	if (p >= 100) return (double) t->rtt_max_us;
	double want = p / 100.0 * t->pongs;
	uint32_t acc = 0;
	for (int i = 0; i < @UNAME@_RTT_BINS; i++) {
		if (!t->rtt_bins[i]) continue;
		if (acc + t->rtt_bins[i] >= want) {
			/* interpolate inside the bin, clamped to what was seen */
			double frac = (want - acc) / t->rtt_bins[i];
			double v = (i + frac) * @UNAME@_RTT_BIN_US;
			if (v < t->rtt_min_us) v = (double) t->rtt_min_us;
			if (v > t->rtt_max_us) v = (double) t->rtt_max_us;
			return v;
		}
		acc += t->rtt_bins[i];
	}
	return (double) t->rtt_max_us;
}

/* the fit's sums with the window still open added, so a run's last second
 * counts too (without closing it: more pongs may land in it) */
bool @NAME@_tx_drift_ppm(const @NAME@_tx_t *t, double *ppm) {
	double n = t->windows, sx = t->sx, sy = t->sy, sxx = t->sxx, sxy = t->sxy;
	if (t->win_any) {
		int64_t x0 = t->windows ? t->x0_us : t->win_start_us, y0 = t->windows ? t->y0_us : t->win_offset_us;
		double x = (t->win_start_us - x0) / 1e6, y = (double) (t->win_offset_us - y0);
		n++;
		sx += x;
		sy += y;
		sxx += x * x;
		sxy += x * y;
	}
	if (n < 3) return false;
	double den = n * sxx - sx * sx;
	if (den <= 0) return false;
	*ppm = (n * sxy - sx * sy) / den; /* us per s */
	return true;
}

double @NAME@_tx_kbps(const @NAME@_tx_t *t) {
	int64_t us = t->last_us - t->first_us;
	if (t->sent < 2 || us <= 0) return 0;
	return t->bytes * 8.0 * 1000.0 / us;
}

size_t @NAME@_tx_json(const @NAME@_tx_t *t, char *out, size_t cap) {
	jw_t jw, *j = &jw;
	jw_begin(j, out, cap);
	jw_int(j, "session", t->session);
	jw_int(j, "sent", t->sent);
	jw_int(j, "send_fail", t->send_fail);
	jw_int(j, "acked", t->acked);
	jw_int(j, "nacked", t->nacked);
	jw_num(j, "seconds", t->sent > 1 ? (t->last_us - t->first_us) / 1e6 : 0, 3);
	jw_num(j, "kbps", @NAME@_tx_kbps(t), 1);
	if (t->pongs || t->stray || t->dup) {
		jw_int(j, "pongs", t->pongs);
		jw_int(j, "lost", t->sent > t->pongs ? t->sent - t->pongs : 0);
		jw_num(j, "loss_pct", t->sent ? 100.0 * (t->sent > t->pongs ? t->sent - t->pongs : 0) / t->sent : 0, 3);
		jw_int(j, "dup", t->dup);
		jw_int(j, "stray", t->stray);
		jw_int(j, "bad", t->bad);
	}
	if (t->pongs) {
		jw_obj(j, "rtt_ms");
		jw_num(j, "min", t->rtt_min_us / 1000.0, 3);
		jw_num(j, "p50", @NAME@_tx_rtt_pct(t, 50) / 1000.0, 3);
		jw_num(j, "p95", @NAME@_tx_rtt_pct(t, 95) / 1000.0, 3);
		jw_num(j, "p99", @NAME@_tx_rtt_pct(t, 99) / 1000.0, 3);
		jw_num(j, "max", t->rtt_max_us / 1000.0, 3);
		jw_num(j, "mean", (double) t->rtt_sum_us / t->pongs / 1000.0, 3);
		jw_obj_end(j);
		jw_obj(j, "clock");
		jw_int(j, "offset_us", (long long) t->offset_us);
		jw_int(j, "offset_delay_us", (long long) t->offset_delay_us);
		jw_int(j, "windows", t->windows + (t->win_any ? 1 : 0)); /* the open one too */
		double ppm;
		if (@NAME@_tx_drift_ppm(t, &ppm))
			jw_num(j, "drift_ppm", ppm, 3);
		else
			jw_null(j, "drift_ppm");
		jw_obj_end(j);
	}
	return jw_end(j);
}

static int hexval(char c) {
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

bool @NAME@_parse_mac(const char *s, uint8_t mac[6]) {
	if (!s) return false;
	size_t n = strlen(s);
	bool sep = n == 17;
	if (!sep && n != 12) return false;
	for (int i = 0; i < 6; i++) {
		const char *p = s + i * (sep ? 3 : 2);
		int hi = hexval(p[0]), lo = hexval(p[1]);
		if (hi < 0 || lo < 0) return false;
		if (sep && i < 5 && p[2] != ':' && p[2] != '-') return false;
		mac[i] = (uint8_t) (hi << 4 | lo);
	}
	return true;
}
