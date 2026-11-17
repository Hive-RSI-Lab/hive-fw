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
 * number, its own length (a truncated frame is caught) and a payload filled
 * with a pattern derived from the sequence, so a corrupt frame is caught
 * rather than counted.
 *
 * Every frame ends with a tag: HMAC-SHA256 of the header under the run's key,
 * cut to @UNAME@_TAG bytes. The payload needs no covering: it is a function
 * of seq and the length, both in the header, and decode checks every byte.
 * A frame whose tag fails is bad before its session or seq is looked at, so a
 * stranger in radio range (any MAC, any seq) can't make a frame of their own:
 * all a forged frame moves is bad. The key is given at the start of each run
 * (from the project's provisioning, never a constant in the code: both ends
 * need the same one, and a fresh one per run); the ipad and opad states are
 * computed once per key, so a frame costs two SHA-256 blocks whatever its
 * length (on the hi-fi Lite lab's ESP32 at 240 MHz, under 190 us a frame).
 *
 * What the tag doesn't stop: a stranger sending again, within the same run,
 * a genuine frame they heard (a replay; across runs it fails, as each run has
 * a fresh key). A replayed DATA frame is a dup (or old, or fills a gap the
 * flooder's real frame left: lost goes down by one, reorder up). A replayed
 * PING is answered once only: the echo remembers the seqs it answered and
 * counts a repeat as ping_dup. A replayed PONG is a dup at the pinger, unless
 * the pinger missed the real one: then it is taken, late, and lowers the
 * loss, raises the round trip and skews that second's clock offset. Nor does
 * it stop jamming: in the lab, 30 forged frames a second (all counted bad)
 * cut a flood's throughput ninefold.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define @UNAME@_HDR 38  /* magic 2, version 1, type 1, length 2, session 4, seq 4, t1 t2 t3 8 each */
#define @UNAME@_TAG 8   /* the HMAC-SHA256 tag at the end of the frame, truncated */
#define @UNAME@_MIN (@UNAME@_HDR + @UNAME@_TAG) /* the shortest frame */
#define @UNAME@_MAX 250 /* ESP-NOW's largest payload */
#define @UNAME@_KEY 16  /* bytes of the run's key */
#define @UNAME@_WINDOW 1024 /* sequence numbers the duplicate check remembers */
#define @UNAME@_RTT_BIN_US 100
#define @UNAME@_RTT_BINS 1000 /* 0 to 100 ms; slower ones land in the last bin */
#define @UNAME@_RTT_MAX_US 10000000 /* a PONG slower than this answers no ping of the run: bad */
#define @UNAME@_T_MAX ((int64_t) 1 << 52) /* a PONG's times past this (142 years of uptime) are bad */

typedef enum { @UNAME@_PING = 1, @UNAME@_PONG = 2, @UNAME@_DATA = 3 } @NAME@_type_t;

typedef struct {
	uint8_t type;
	uint32_t session, seq;
	int64_t t1, t2, t3; /* microseconds on the clock of the board that wrote each */
	uint16_t len;       /* the whole frame with its tag, @UNAME@_MIN..@UNAME@_MAX */
} @NAME@_frame_t;

typedef struct {
	uint32_t h[8];
	uint64_t len;
	uint8_t buf[64];
	size_t n;
} @NAME@_sha256_t;

/* the run's key, ready to tag with: the HMAC inner and outer states. Until
 * key_init (and after key_wipe) it isn't ready, and encode and decode refuse
 * it: an all-zero state is a key anyone could compute. */
typedef struct {
	@NAME@_sha256_t in, out;
	bool ready;
} @NAME@_key_t;

void @NAME@_key_init(@NAME@_key_t *k, const uint8_t key[@UNAME@_KEY]);
void @NAME@_key_wipe(@NAME@_key_t *k);
/* zeroes n bytes in a way the compiler keeps, for key bytes on their way out */
void @NAME@_wipe(void *p, size_t n);
/* 32 hex digits; false (key untouched) for anything else */
bool @NAME@_parse_key(const char *s, uint8_t key[@UNAME@_KEY]);
/* the tag of a frame's header (its first @UNAME@_HDR bytes) */
void @NAME@_tag(const @NAME@_key_t *k, const uint8_t *hdr, uint8_t tag[@UNAME@_TAG]);

/* the frame's bytes, f->len of them (payload filled, tag at the end); 0 when
 * it doesn't fit or k isn't ready */
size_t @NAME@_encode(const @NAME@_key_t *k, const @NAME@_frame_t *f, uint8_t *buf, size_t cap);
/* false: not one of ours, damaged, or not tagged with this key */
bool @NAME@_decode(const @NAME@_key_t *k, const uint8_t *buf, size_t n, @NAME@_frame_t *f);

typedef struct {
	uint32_t top; /* the highest sequence number seen + 1; 0 none */
	uint64_t bits[@UNAME@_WINDOW / 64];
} @NAME@_seen_t;

/* 0 new, 1 a duplicate, -1 too old to tell (and seq UINT32_MAX, which
 * would wrap top) */
int @NAME@_seen(@NAME@_seen_t *s, uint32_t seq);

/* ------------------------------------------------------------ receiving end */

typedef struct {
	uint32_t session;
	uint32_t rx, dup, reorder, old, bad;
	uint32_t stray; /* DATA of another session while this one is live */
	uint32_t pings; /* PINGs answered (echo) */
	uint32_t ping_dup; /* PINGs already answered: a replay gets no second PONG */
	uint64_t bytes;
	int64_t first_us, last_us;
	@NAME@_seen_t seen;
	uint32_t ping_session; /* the session of the PINGs in ping_seen */
	@NAME@_seen_t ping_seen;
} @NAME@_rx_t;

void @NAME@_rx_init(@NAME@_rx_t *r);
/* a DATA frame. The first frame's session is adopted; a frame of another
 * session starts the counts again only once this one has been silent for
 * @UNAME@_SESSION_IDLE_US (a late frame of the last run is counted as
 * stray, not taken for a new run: sessions are random, not ordered). */
#define @UNAME@_SESSION_IDLE_US 1000000
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
/* a PONG that arrived at t4. One whose times no echo could have written (t1
 * after t4, t3 before t2, a negative time or round trip, a round trip over
 * @UNAME@_RTT_MAX_US, any time past @UNAME@_T_MAX) counts as bad. */
void @NAME@_tx_pong(@NAME@_tx_t *t, const @NAME@_frame_t *f, int64_t t4);
/* round trip at percentile p (0..100), in microseconds; -1 none */
double @NAME@_tx_rtt_pct(const @NAME@_tx_t *t, double p);
/* drift of the echo's clock against ours in ppm, the open window counted;
 * false: under 3 windows */
bool @NAME@_tx_drift_ppm(const @NAME@_tx_t *t, double *ppm);
double @NAME@_tx_kbps(const @NAME@_tx_t *t);
/* the sender's result as one JSON object; the length, or 0 when cap is too small (512 is plenty) */
size_t @NAME@_tx_json(const @NAME@_tx_t *t, char *out, size_t cap);

/* ------------------------------------------------------- one received frame */

typedef enum {
	@UNAME@_GOT_USED,  /* counted (DATA, a PONG) */
	@UNAME@_GOT_REPLY, /* a PING: send *pong back (its t3 stamped at the send) */
	@UNAME@_GOT_BAD,   /* not one of ours, or damaged: counted in rx->bad */
	@UNAME@_GOT_OTHER, /* a good frame this end has no use for (or a PING it answered already) */
} @NAME@_got_t;

/* A radio callback's copy of a received value into its queue item's buffer
 * (cap bytes): the length copied, 0 (nothing copied) when there is nothing
 * or it is longer than cap. The item then goes to @NAME@_receive, where a
 * length of 0 counts as bad. */
size_t @NAME@_take(uint8_t *dst, size_t cap, const uint8_t *src, size_t n);

/* What an end does with the bytes of one frame from the radio (n as
 * received: never trusted). An echo end answers PINGs and counts DATA into
 * rx; an end that takes PONGs (pongs) counts them into tx. rx is always
 * needed, tx only with pongs. t_us: the arrival time. A frame not tagged
 * with k is bad, whatever it says. The radio task and the fuzzer both call
 * this, so what is fuzzed is what runs. */
@NAME@_got_t @NAME@_receive(const @NAME@_key_t *k, @NAME@_rx_t *rx, @NAME@_tx_t *tx, bool echo, bool pongs,
                            const uint8_t *buf, size_t n, int64_t t_us, @NAME@_frame_t *pong);

/* "30:76:f5:2d:74:3c", "30-76-..." or "3076f52d743c" (a board's device id) */
bool @NAME@_parse_mac(const char *s, uint8_t mac[6]);
