/*
 * The board's WebSocket to the Hive (rr_ws.h). Built on tcp_transport
 * rather than esp_websocket_client: in IDF 4.4 that client can't take the
 * certificate bundle, and without a CA it doesn't check the server at all.
 * Here the SSL transport carries esp_crt_bundle_attach, so the Cloudflare
 * edge's certificate is checked like any https:// site's. The WebSocket on
 * top is our own (rr_ws_proto.c), not transport_ws: that one reads
 * the upgrade answer in one gulp and drops whatever followed it in the same
 * TLS record, so a command sent the moment the board connected put every
 * later frame out of step.
 *
 * mbedTLS isn't safe from two tasks at once, so every read and write on the
 * socket holds `lock`; the task waits for data (poll) without it.
 */
#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_transport.h"
#include "esp_transport_ssl.h"
#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR >= 5
#include "esp_random.h"
#else
#include "esp_system.h"
#endif
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "rr_ws.h"
#include "rr_ws_proto.h"

#if ESP_IDF_VERSION_MAJOR >= 5
#define SHA1 mbedtls_sha1
#else
#define SHA1 mbedtls_sha1_ret
#endif

static const char TAG[] = "rr_ws";

#define PING_S 20       /* the endpoint drops a board it hasn't heard from in 90 s */
#define BACKOFF_MIN_S 5
#define BACKOFF_MAX_S 300
#define IO_MS 10000
#define STACK 6144     /* bytes: a TLS handshake with the certificate bundle */

static SemaphoreHandle_t lock;
static SemaphoreHandle_t done, go; /* go: the parked task starts a link */
static TaskHandle_t task;
static esp_transport_handle_t ssl;
static char url[128], *headers;
static size_t msg_max;
static rr_ws_msg_cb_t on_msg;
static rr_ws_up_cb_t on_up;
static volatile bool running, up, broken;
static int64_t last_tx;
static rr_ws_stats_t stats; /* rr_ws_stats(); 64-bit fields, so under stats_mux */
static portMUX_TYPE stats_mux = portMUX_INITIALIZER_UNLOCKED;

static int64_t now_s(void) { return esp_timer_get_time() / 1000000; }

static void drop(void) {
	xSemaphoreTake(lock, portMAX_DELAY);
	if (up) {
		portENTER_CRITICAL(&stats_mux);
		stats.drops++;
		stats.up_since_us = 0;
		portEXIT_CRITICAL(&stats_mux);
	}
	up = false;
	if (ssl) {
		esp_transport_close(ssl);
		esp_transport_destroy(ssl);
	}
	ssl = NULL;
	xSemaphoreGive(lock);
}

/* n bytes or nothing: a short read mid-frame would put the stream out of step */
static bool read_exact(void *b, size_t n) {
	size_t got = 0;
	while (got < n) {
		int r = esp_transport_read(ssl, (char *) b + got, (int) (n - got), IO_MS);
		if (r <= 0) return false;
		got += (size_t) r;
		portENTER_CRITICAL(&stats_mux);
		stats.rx_bytes += (uint64_t) r;
		portEXIT_CRITICAL(&stats_mux);
	}
	return true;
}

static bool write_all(const void *b, size_t n) {
	size_t put = 0;
	while (put < n) {
		int r = esp_transport_write(ssl, (const char *) b + put, (int) (n - put), IO_MS);
		if (r <= 0) return false;
		put += (size_t) r;
		portENTER_CRITICAL(&stats_mux);
		stats.tx_bytes += (uint64_t) r;
		portEXIT_CRITICAL(&stats_mux);
	}
	return true;
}

/* One masked frame, header and payload in one write (one TLS record). Holds no lock. */
static bool write_frame(int op, const char *data, size_t len) {
	uint8_t mask[4];
	esp_fill_random(mask, sizeof(mask));
	uint8_t *f = malloc(RR_WS_HEAD_MAX + len);
	if (!f) return false;
	size_t h = rr_ws_head(f, op, len, mask);
	for (size_t i = 0; i < len; i++) f[h + i] = (uint8_t) data[i] ^ mask[i & 3];
	bool ok = write_all(f, h + len);
	free(f);
	return ok;
}

static bool upgrade(const char *host, int port, const char *path) {
	uint8_t raw[16];
	unsigned char key[32], acc[32], sha[20];
	size_t kn = 0, an = 0;
	esp_fill_random(raw, sizeof(raw));
	if (mbedtls_base64_encode(key, sizeof(key), &kn, raw, sizeof(raw))) return false;
	key[kn] = '\0';
	char *req = malloc(512 + strlen(headers));
	if (!req) return false;
	int n = sprintf(req, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
			"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: rr-board\r\n%s\r\n",
			path, host, (char *) key, headers);
	bool ok = write_all(req, (size_t) n);
	free(req);
	if (!ok) return false;
	/* the answer a byte at a time, so nothing after its blank line is read here */
	char *resp = malloc(2048);
	if (!resp) return false;
	size_t got = 0;
	while (got < 2047 && !(got >= 4 && !memcmp(resp + got - 4, "\r\n\r\n", 4))) {
		if (!read_exact(resp + got, 1)) break;
		got++;
	}
	resp[got] = '\0';
	char magic[64];
	snprintf(magic, sizeof(magic), "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", (char *) key);
	ok = got >= 4 && !memcmp(resp + got - 4, "\r\n\r\n", 4) && !SHA1((const unsigned char *) magic, strlen(magic), sha) &&
	     !mbedtls_base64_encode(acc, sizeof(acc), &an, sha, sizeof(sha));
	if (ok) {
		acc[an] = '\0';
		ok = rr_ws_upgrade_ok(resp, (char *) acc);
	}
	if (!ok) {
		char *e = strstr(resp, "\r\n");
		if (e) *e = '\0';
		ESP_LOGW(TAG, "the endpoint refused the link: %.60s", resp[0] ? resp : "(no answer)");
	}
	free(resp);
	(void) port;
	return ok;
}

static bool dial(void) {
	char host[RR_WS_HOST_MAX], path[RR_WS_PATH_MAX];
	int port;
	if (rr_ws_url_parse(url, host, &port, path)) return false;
	xSemaphoreTake(lock, portMAX_DELAY);
	ssl = esp_transport_ssl_init();
	xSemaphoreGive(lock);
	if (!ssl) return false;
	esp_transport_ssl_crt_bundle_attach(ssl, esp_crt_bundle_attach);
	esp_transport_keep_alive_t ka = {.keep_alive_enable = true, .keep_alive_idle = 30, .keep_alive_interval = 10, .keep_alive_count = 3};
	esp_transport_ssl_set_keep_alive(ssl, &ka);
	if (esp_transport_connect(ssl, host, port, IO_MS) < 0) return false;
	return upgrade(host, port, path);
}

/* One frame in, whole. A text message goes to on_msg; a ping is answered; a
 * close or a broken read ends the link; anything else is read and dropped. */
static void read_frame(void) {
	uint8_t hb[RR_WS_HEAD_MAX];
	rr_ws_head_t h;
	char *m = NULL;
	bool take = false;
	xSemaphoreTake(lock, portMAX_DELAY);
	if (!read_exact(hb, 2) || !read_exact(hb + 2, rr_ws_head_len(hb) - 2) || rr_ws_head_parse(hb, &h)) {
		broken = true;
		goto out;
	}
	take = h.op == 1 && h.fin && h.len > 0 && h.len <= msg_max;
	bool ctl = h.op >= 8;
	if (take || ctl) {
		m = malloc((size_t) h.len + 1);
		if (!m || !read_exact(m, (size_t) h.len)) {
			broken = true;
			goto out;
		}
		for (size_t i = 0; i < h.len; i++) m[i] ^= (char) h.mask[i & 3];
		m[h.len] = '\0';
	} else {
		char sink[128];
		for (uint64_t left = h.len; left;) {
			size_t n = left < sizeof(sink) ? (size_t) left : sizeof(sink);
			if (!read_exact(sink, n)) {
				broken = true;
				goto out;
			}
			left -= n;
		}
		ESP_LOGW(TAG, "dropped a %u-byte frame (opcode %d%s)", (unsigned) h.len, h.op, h.fin ? "" : ", split");
		goto out;
	}
	if (h.op == 9) {
		if (!write_frame(10, m, (size_t) h.len)) broken = true;
		last_tx = now_s();
	} else if (h.op == 8) {
		unsigned code = h.len >= 2 ? (unsigned) ((uint8_t) m[0] << 8 | (uint8_t) m[1]) : 1005;
		ESP_LOGW(TAG, "the endpoint closed the link (%u)", code);
		write_frame(8, m, h.len >= 2 ? 2 : 0);
		broken = true;
	}
out:
	xSemaphoreGive(lock);
	if (m && take && !broken) {
		on_msg(m, (size_t) h.len);
		return;
	}
	free(m);
}

/* One task for the board's life, parked on `go` while the link is off: a
 * task that exited and was made again would need a fresh 6 KB stack each
 * time, and a busy board's internal RAM can have no 6 KB block left, so the
 * link would never start. Its stack is in PSRAM where the board has some
 * (nothing on it touches flash: messages go to the relay's worker, which
 * has an internal stack), else in internal RAM. */
static void ws_task(void *arg) {
	for (;;) {
		xSemaphoreTake(go, portMAX_DELAY);
		int backoff = BACKOFF_MIN_S;
		while (running) {
			broken = false;
			if (!dial()) {
				drop();
				ESP_LOGW(TAG, "couldn't reach %s; again in %d s", url, backoff);
				for (int i = 0; i < backoff * 2 && running; i++) vTaskDelay(pdMS_TO_TICKS(500));
				backoff = backoff * 2 > BACKOFF_MAX_S ? BACKOFF_MAX_S : backoff * 2;
				continue;
			}
			backoff = BACKOFF_MIN_S;
			ESP_LOGI(TAG, "up: %s", url);
			last_tx = now_s();
			portENTER_CRITICAL(&stats_mux);
			stats.up_since_us = esp_timer_get_time();
			if (!stats.first_up_us) stats.first_up_us = stats.up_since_us;
			portEXIT_CRITICAL(&stats_mux);
			up = true;
			if (on_up) on_up();
			while (running && !broken) {
				if (now_s() - last_tx >= PING_S) {
					xSemaphoreTake(lock, portMAX_DELAY);
					if (!write_frame(9, NULL, 0)) broken = true;
					last_tx = now_s();
					xSemaphoreGive(lock);
				}
				int p = esp_transport_poll_read(ssl, 500);
				if (p < 0) break;
				if (p > 0) read_frame(); /* a socket the far end closed reads as broken */
			}
			drop();
			if (running) {
				ESP_LOGW(TAG, "link dropped; again in %d s", BACKOFF_MIN_S);
				for (int i = 0; i < BACKOFF_MIN_S * 2 && running; i++) vTaskDelay(pdMS_TO_TICKS(500));
			}
		}
		xSemaphoreGive(done);
	}
}
static bool task_make(void) {
	StackType_t *stack = heap_caps_malloc(STACK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
	StaticTask_t *tcb = heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
	if (stack && tcb) task = xTaskCreateStatic(ws_task, "rr_ws", STACK, NULL, tskIDLE_PRIORITY + 2, stack, tcb);
	if (task) return true;
	free(stack);
	free(tcb);
	if (xTaskCreate(ws_task, "rr_ws", STACK, NULL, tskIDLE_PRIORITY + 2, &task) == pdPASS) return true;
	task = NULL;
	ESP_LOGE(TAG, "no memory for the link's task (internal heap %u B free, largest block %u B)",
		 (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL), (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
	return false;
}

bool rr_ws_start(const char *u, const char *h, size_t max, rr_ws_msg_cb_t m, rr_ws_up_cb_t cb) {
	if (running || !m || strlen(u) >= sizeof(url)) return false;
	if (!lock) lock = xSemaphoreCreateMutex();
	if (!done) done = xSemaphoreCreateBinary();
	if (!go) go = xSemaphoreCreateBinary();
	if (!lock || !done || !go) return false;
	if (!task && !task_make()) return false;
	free(headers);
	headers = strdup(h ? h : "");
	if (!headers) return false;
	strcpy(url, u);
	msg_max = max;
	on_msg = m;
	on_up = cb;
	running = true;
	xSemaphoreGive(go);
	return true;
}

void rr_ws_stop(void) {
	if (!running) return;
	running = false;
	xSemaphoreTake(done, portMAX_DELAY); /* at most a dial's or a read's timeout */
	free(headers);
	headers = NULL;
}

bool rr_ws_up(void) { return up; }

void rr_ws_stats(rr_ws_stats_t *out) {
	portENTER_CRITICAL(&stats_mux);
	*out = stats;
	portEXIT_CRITICAL(&stats_mux);
}

bool rr_ws_send(const char *text, size_t len) {
	if (!up || !len) return false;
	xSemaphoreTake(lock, portMAX_DELAY);
	bool ok = up && ssl && write_frame(1, text, len);
	if (ok) last_tx = now_s();
	else broken = up;
	xSemaphoreGive(lock);
	return ok;
}
