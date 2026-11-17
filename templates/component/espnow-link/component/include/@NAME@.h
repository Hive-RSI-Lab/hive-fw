/*
 * Link measurement between two boards, whatever carries the frames
 * (ESP-NOW, a BLE GATT characteristic, UDP): the frame both ends speak and
 * the statistics each end keeps. Plain C, so the host tests run it.
 *
 * A pinger sends PING frames carrying its send time t1; the echo writes its
 * receive time t2 and its send time t3 into a PONG; the pinger notes t4 on
 * receipt. That gives the round trip (t4 - t1) - (t3 - t2) and, as NTP does,
 * the echo's clock offset ((t2 - t1) + (t3 - t4)) / 2. The lowest-delay
 * sample of each second goes into a least-squares line, whose slope is the
 * drift between the two crystals in ppm: what synced multiroom audio needs.
 *
 * A flooder sends DATA frames as fast as it is let; the sink counts what
 * arrived, duplicates, reordering and gaps, and the bytes per second.
 *
 * Frames carry a session number (a new run starts a new one), a sequence
 * number and a payload filled with a pattern derived from the sequence, so
 * a corrupt frame is caught rather than counted.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define @UNAME@_HDR 36  /* magic 2, version 1, type 1, session 4, seq 4, t1 t2 t3 8 each */
#define @UNAME@_MAX 250 /* ESP-NOW's largest payload */
#define @UNAME@_WINDOW 1024 /* sequence numbers the duplicate check remembers */
#define @UNAME@_RTT_BIN_US 100
#define @UNAME@_RTT_BINS 1000 /* 0 to 100 ms; slower ones land in the last bin */

typedef enum { @UNAME@_PING = 1, @UNAME@_PONG = 2, @UNAME@_DATA = 3 } @NAME@_type_t;

typedef struct {
	uint8_t type;
	uint32_t session, seq;
	int64_t t1, t2, t3; /* microseconds on the clock of the board that wrote each */
	uint16_t len;       /* the whole frame, @UNAME@_HDR..@UNAME@_MAX */
} @NAME@_frame_t;

/* the frame's bytes, f->len of them (the payload filled); 0 when it doesn't fit */
size_t @NAME@_encode(const @NAME@_frame_t *f, uint8_t *buf, size_t cap);
/* false: not one of ours, or damaged */
bool @NAME@_decode(const uint8_t *buf, size_t n, @NAME@_frame_t *f);

typedef struct {
	uint32_t top; /* the highest sequence number seen + 1; 0 none */
	uint64_t bits[@UNAME@_WINDOW / 64];
} @NAME@_seen_t;

/* 0 new, 1 a duplicate, -1 too old to tell */
int @NAME@_seen(@NAME@_seen_t *s, uint32_t seq);

/* ------------------------------------------------------------ receiving end */

typedef struct {
	uint32_t session;
	uint32_t rx, dup, reorder, old, bad;
	uint32_t pings; /* PINGs answered (echo) */
	uint64_t bytes;
	int64_t first_us, last_us;
	@NAME@_seen_t seen;
} @NAME@_rx_t;

void @NAME@_rx_init(@NAME@_rx_t *r);
/* a DATA frame (a frame of a new session starts the counts again) */
void @NAME@_rx_data(@NAME@_rx_t *r, const @NAME@_frame_t *f, int64_t now_us);
/* a frame that failed to decode */
void @NAME@_rx_bad(@NAME@_rx_t *r);
uint32_t @NAME@_rx_expected(const @NAME@_rx_t *r);
double @NAME@_rx_kbps(const @NAME@_rx_t *r);
/* the sink's result as one JSON object; the length, or 0 when cap is too small */
size_t @NAME@_rx_json(const @NAME@_rx_t *r, char *out, size_t cap);

/* An echo's answer: the PONG for a PING, t2 the time it arrived and t3 now */
void @NAME@_pong_for(const @NAME@_frame_t *ping, int64_t t2, int64_t t3, @NAME@_frame_t *pong);

/* ------------------------------------------------------------ sending end */

typedef struct {
	uint32_t session;
	uint32_t sent, send_fail; /* handed to the radio; refused by it */
	uint32_t acked, nacked;   /* the radio's own (MAC) acknowledgement */
	uint32_t pongs, dup, stray, bad;
	uint64_t bytes;
	int64_t first_us, last_us;
	/* round trips */
	uint32_t rtt_bins[@UNAME@_RTT_BINS];
	int64_t rtt_min_us, rtt_max_us, rtt_sum_us;
	/* clock offset and drift */
	int64_t win_start_us;          /* t1 of the current second's window */
	int64_t win_delay_us, win_offset_us;
	bool win_any;
	int64_t x0_us, y0_us;          /* the first window's t1 and offset: x, y are relative */
	double sx, sy, sxx, sxy;       /* least squares of offset (us) on time (s) */
	uint32_t windows;
	int64_t offset_us;             /* the newest window's best offset */
	int64_t offset_delay_us;
	@NAME@_seen_t seen;
} @NAME@_tx_t;

void @NAME@_tx_init(@NAME@_tx_t *t, uint32_t session);
/* a frame handed to the radio (ok) or refused by it, at now_us */
void @NAME@_tx_sent(@NAME@_tx_t *t, size_t len, bool ok, int64_t now_us);
/* the radio's send report: acknowledged by the peer or not */
void @NAME@_tx_acked(@NAME@_tx_t *t, bool acked);
/* a PONG that arrived at t4 */
void @NAME@_tx_pong(@NAME@_tx_t *t, const @NAME@_frame_t *f, int64_t t4);
/* round trip at percentile p (0..100), in microseconds; -1 none */
double @NAME@_tx_rtt_pct(const @NAME@_tx_t *t, double p);
/* drift of the echo's clock against ours in ppm; false: under 3 windows */
bool @NAME@_tx_drift_ppm(const @NAME@_tx_t *t, double *ppm);
double @NAME@_tx_kbps(const @NAME@_tx_t *t);
/* the sender's result as one JSON object; the length, or 0 when cap is too small (512 is plenty) */
size_t @NAME@_tx_json(const @NAME@_tx_t *t, char *out, size_t cap);

/* "30:76:f5:2d:74:3c", "30-76-..." or "3076f52d743c" (a board's device id) */
bool @NAME@_parse_mac(const char *s, uint8_t mac[6]);
