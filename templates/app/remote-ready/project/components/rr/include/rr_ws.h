/*
 * The board's WebSocket to the Hive's bench endpoint: one task that dials wss:// with the dial's
 * headers (rr_ws_proto.c), keeps the link with a ping every 20 s,
 * hands each text message to the relay and redials with a backoff when the
 * link drops. TLS is checked against the certificate bundle.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A whole text message from the Hive, malloc'd and NUL-terminated: the
 * callee frees it. */
typedef void (*rr_ws_msg_cb_t)(char *data, size_t len);
/* Each time the link comes up (the board sends its status then). */
typedef void (*rr_ws_up_cb_t)(void);

/* Starts the link; url and headers are copied. msg_max is the biggest
 * message taken (a bigger one is read and dropped). False if it can't start. */
bool rr_ws_start(const char *url, const char *headers, size_t msg_max, rr_ws_msg_cb_t on_msg, rr_ws_up_cb_t on_up);
/* Closes the link and waits for its task to end. */
void rr_ws_stop(void);
/* True while the link is up. */
bool rr_ws_up(void);
/* Sends one text message; false if the link is down or the send failed. */
bool rr_ws_send(const char *text, size_t len);

/* The link's counters since boot, for diagnostics: bytes on the TLS
 * socket (frames and their headers; not the TLS overhead), the times an up
 * link dropped, and when it came up (esp_timer µs; 0: not up). */
typedef struct {
	uint64_t tx_bytes, rx_bytes;
	uint32_t drops;
	int64_t up_since_us, first_up_us;
} rr_ws_stats_t;
void rr_ws_stats(rr_ws_stats_t *out);
