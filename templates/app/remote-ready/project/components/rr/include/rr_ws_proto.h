/*
 * The Wi-Fi link's plain parts: the board dials the Hive's bench endpoint
 * through Cloudflare over a WebSocket. Its URL, the headers of its dial and
 * the "t" tag on each message it sends are built here, tested on a PC; the
 * socket itself is rr_ws.c.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RR_WS_HOST_MAX 64
#define RR_WS_PATH_MAX 64
#define RR_WS_HEADERS_MAX 320

/* Splits wss://host[:port][/path]. Only wss:// (TLS) is taken; the host is
 * letters, digits, dots and dashes; the port defaults to 443; the path to
 * "/" and has no spaces or control characters. Returns 0, or -1 for a URL
 * the board won't dial. */
int rr_ws_url_parse(const char *url, char host[RR_WS_HOST_MAX], int *port, char path[RR_WS_PATH_MAX]);

/* True for the board's own key from the Hive: exactly 64 hex digits. */
bool rr_ws_key_ok(const char *key);

/*
 * The dial's extra headers, each "Name: value\r\n": the Cloudflare service
 * token (CF-Access-Client-Id/-Secret, left out when id is empty), then
 * X-Bench-Dev and X-Bench-Key. Returns the length written into out, or -1
 * when a value is empty where it is needed, too long, or holds a character
 * that could start another header (CR, LF or any other control character).
 */
int rr_ws_headers(char *out, size_t cap, const char *id, const char *secret, const char *dev, const char *key);

/*
 * Tags a JSON object the board sends ("{...}", len bytes, NUL-terminated in
 * a buffer of cap bytes) with "t":"<t>" as its first member, in place, so
 * the endpoint can tell a status, an event and a reply apart on one socket.
 * Returns the new length, or 0 if it isn't an object or doesn't fit.
 */
size_t rr_ws_tag(char *buf, size_t len, size_t cap, const char *t);

/*
 * The WebSocket framing itself (RFC 6455), done here rather than by IDF 4.4's
 * transport_ws: that one reads the server's 101 answer in one gulp and loses
 * whatever came in the same TLS record after it, so a command the Hive sends
 * the moment the board connects cut the stream and every frame after it was
 * misread.
 */
#define RR_WS_HEAD_MAX 14

typedef struct {
	int op;            /* 1 text, 2 binary, 8 close, 9 ping, 10 pong */
	bool fin, masked;
	uint8_t mask[4];
	uint64_t len;      /* the payload's */
} rr_ws_head_t;

/* A client frame's header: FIN, op, masked with mask. Returns its length. */
size_t rr_ws_head(uint8_t out[RR_WS_HEAD_MAX], int op, size_t len, const uint8_t mask[4]);
/* The whole header's length from its first two bytes (2 to 14). */
size_t rr_ws_head_len(const uint8_t b[2]);
/* Reads a whole header (rr_ws_head_len bytes). -1 if a reserved bit is
 * set or a control frame is longer than 125 bytes or split. */
int rr_ws_head_parse(const uint8_t *b, rr_ws_head_t *h);
/* The server's answer to the upgrade, NUL-terminated: true only for a 101
 * whose Sec-WebSocket-Accept (header name in any case) is accept. */
bool rr_ws_upgrade_ok(const char *resp, const char *accept);
