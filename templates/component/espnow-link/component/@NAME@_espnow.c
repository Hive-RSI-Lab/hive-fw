/* @NAME@ over ESP-NOW: the radio side of @NAME@.h (see @NAME@_espnow.h) */
#include "@NAME@_espnow.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
#include "esp_mac.h"    /* MACSTR */
#include "esp_random.h"
#endif
#include "esp_now.h"
#include "esp_system.h"
#include "esp_task.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "@NAME@";

typedef struct {
	uint8_t len;
	int64_t t_us;
	uint8_t data[@UNAME@_MAX];
} rx_item_t;

#define QLEN 32
#define STACK 4096

/* Made once and kept: a callback still running as ESP-NOW comes down never
 * touches freed memory. The big parts (the queue's QLEN frames, ~8 KB, the
 * 4 KB stack, the ~4 KB histogram) go to PSRAM when the board has it: on the
 * hi-fi Lite, Wi-Fi + Bluetooth + audio left ~13 KB of internal RAM, and an
 * internal queue and stack would have taken nearly all of it. The worker is
 * made once and waits for runs, so its static stack is never reused under a
 * task still being deleted. A task whose stack is in PSRAM must not write
 * flash (NVS); this one doesn't. */
static QueueHandle_t q;
static StaticQueue_t q_ctl;
static TaskHandle_t worker;
static StaticTask_t worker_tcb;
static SemaphoreHandle_t window_sem;
static @NAME@_tx_t *tx;
static @NAME@_rx_t *rx;

static volatile bool run, alive;
static @NAME@_espnow_cfg_t cfg;
static uint8_t channel;
static bool rate_changed;
static volatile uint32_t q_full, other;

/* the callbacks' shapes changed in IDF 5.0 (receive) and 5.5 (send) */
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
static void recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int n) {
	const uint8_t *mac = info->src_addr;
#else
static void recv_cb(const uint8_t *mac, const uint8_t *data, int n) {
#endif
	int64_t t = esp_timer_get_time();
	if (!run || n <= 0 || n > @UNAME@_MAX) return;
	if (memcmp(mac, cfg.peer, 6)) { /* only the peer's frames are looked at */
		other++;
		return;
	}
	rx_item_t it = {.len = (uint8_t) n, .t_us = t};
	memcpy(it.data, data, n);
	if (xQueueSend(q, &it, 0) != pdTRUE) q_full++;
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void send_cb(const esp_now_send_info_t *info, esp_now_send_status_t st) {
#else
static void send_cb(const uint8_t *mac, esp_now_send_status_t st) {
#endif
	if (!run) return;
	@NAME@_tx_acked(tx, st == ESP_NOW_SEND_SUCCESS);
	if (cfg.role == @UNAME@_ROLE_FLOOD) xSemaphoreGive(window_sem);
}

static bool send_frame(@NAME@_frame_t *f) {
	uint8_t buf[@UNAME@_MAX];
	int64_t t = esp_timer_get_time();
	if (f->type == @UNAME@_PING) f->t1 = t;
	if (f->type == @UNAME@_PONG) f->t3 = t;
	size_t n = @NAME@_encode(f, buf, sizeof(buf));
	if (!n) return false;
	esp_err_t e = esp_now_send(cfg.peer, buf, n);
	@NAME@_tx_sent(tx, n, e == ESP_OK, t);
	return e == ESP_OK;
}

static void handle(const rx_item_t *it) {
	@NAME@_frame_t f;
	if (!@NAME@_decode(it->data, it->len, &f)) {
		@NAME@_rx_bad(rx);
		return;
	}
	if (cfg.role == @UNAME@_ROLE_ECHO && f.type == @UNAME@_PING) {
		@NAME@_frame_t pong;
		@NAME@_pong_for(&f, it->t_us, 0, &pong);
		rx->pings++;
		send_frame(&pong);
	} else if (cfg.role == @UNAME@_ROLE_ECHO && f.type == @UNAME@_DATA)
		@NAME@_rx_data(rx, &f, it->t_us);
	else if (cfg.role == @UNAME@_ROLE_PING && f.type == @UNAME@_PONG)
		@NAME@_tx_pong(tx, &f, it->t_us);
	else
		other++;
}

static void teardown(void) {
	run = false;
	esp_now_unregister_recv_cb();
	esp_now_unregister_send_cb();
	esp_now_deinit();
	if (rate_changed) esp_wifi_config_espnow_rate(WIFI_IF_STA, WIFI_PHY_RATE_1M_L);
	rate_changed = false;
}

static void *big(size_t n) { /* PSRAM when there is some, else internal */
	return heap_caps_malloc_prefer(n, 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

static void *stack_mem(size_t n) {
#if CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY
	return big(n);
#else
	return heap_caps_malloc(n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
#endif
}

static void one_run(void) {
	rx_item_t it;
	uint32_t seq = 0;
	int64_t period = 1000000 / cfg.rate_hz;
	int64_t next = esp_timer_get_time(), done_at = 0;
	@NAME@_frame_t f = {.session = tx->session, .len = (uint16_t) cfg.len};
	while (run) {
		int64_t now = esp_timer_get_time();
		if (cfg.role == @UNAME@_ROLE_FLOOD) {
			if (cfg.count && seq >= cfg.count) {
				if (!done_at) done_at = now + 500000; /* let the last send reports in */
				if (now >= done_at) break;
				vTaskDelay(pdMS_TO_TICKS(20));
				continue;
			}
			xSemaphoreTake(window_sem, pdMS_TO_TICKS(200)); /* a lost report must not stall the flood */
			f.type = @UNAME@_DATA;
			f.seq = seq;
			if (send_frame(&f))
				seq++;
			else {
				xSemaphoreGive(window_sem);
				vTaskDelay(1);
			}
			continue;
		}
		TickType_t wait = pdMS_TO_TICKS(100);
		if (cfg.role == @UNAME@_ROLE_PING && !done_at) {
			if (now >= next) {
				if (cfg.count && seq >= cfg.count)
					done_at = now + 1000000; /* a second for the last pongs */
				else {
					f.type = @UNAME@_PING;
					f.seq = seq++;
					f.t2 = f.t3 = 0;
					send_frame(&f);
					next += period;
					if (next < now) next = now + period; /* fell behind: don't burst */
				}
				continue;
			}
			wait = (next - now) / 1000 / portTICK_PERIOD_MS;
			if (wait < 1) wait = 1; /* never spin: idle on this core feeds the watchdog */
		}
		if (done_at && now >= done_at) break;
		if (xQueueReceive(q, &it, wait) == pdTRUE) handle(&it);
	}
	teardown();
	ESP_LOGI(TAG, "stopped: sent %u, pongs %u, rx %u", (unsigned) tx->sent, (unsigned) tx->pongs, (unsigned) rx->rx);
}

static void task_main(void *arg) {
	for (;;) {
		ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
		one_run(); /* a run stopped before it began still tears ESP-NOW down */
		alive = false;
	}
}

esp_err_t @NAME@_espnow_start(const @NAME@_espnow_cfg_t *c) {
	if (alive) return ESP_ERR_INVALID_STATE;
	cfg = *c;
	if (!cfg.rate_hz) cfg.rate_hz = 20;
	if (!cfg.len) cfg.len = cfg.role == @UNAME@_ROLE_FLOOD ? @UNAME@_MAX : 64;
	if (!cfg.window) cfg.window = 4;
	if (cfg.role > @UNAME@_ROLE_FLOOD || cfg.rate_hz > 100 || cfg.len < @UNAME@_HDR || cfg.len > @UNAME@_MAX ||
	    cfg.window > 16)
		return ESP_ERR_INVALID_ARG;
	if (!worker) {
		uint8_t *qmem = big(QLEN * sizeof(rx_item_t));
		StackType_t *stack = stack_mem(STACK);
		if (!window_sem) window_sem = xSemaphoreCreateCounting(16, 0);
		if (!tx) tx = big(sizeof(*tx)); /* ~4 KB: the round-trip histogram */
		if (!rx) rx = big(sizeof(*rx));
		if (!qmem || !stack || !window_sem || !tx || !rx) {
			free(qmem);
			free(stack);
			return ESP_ERR_NO_MEM;
		}
		q = xQueueCreateStatic(QLEN, sizeof(rx_item_t), qmem, &q_ctl);
		worker = xTaskCreateStaticPinnedToCore(task_main, "@NAME@", STACK, NULL, ESP_TASK_PRIO_MIN + 5, stack,
						       &worker_tcb, 0);
		if (!worker) return ESP_ERR_NO_MEM;
	}
	xQueueReset(q);
	while (xSemaphoreTake(window_sem, 0) == pdTRUE)
		;
	for (uint32_t i = 0; i < cfg.window; i++) xSemaphoreGive(window_sem);
	@NAME@_tx_init(tx, esp_random());
	@NAME@_rx_init(rx);
	q_full = other = 0;

	wifi_second_chan_t second;
	if (esp_wifi_get_channel(&channel, &second) != ESP_OK) channel = 0;
	esp_err_t e = esp_now_init();
	if (e != ESP_OK) return e;
	esp_now_peer_info_t p = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
	memcpy(p.peer_addr, cfg.peer, 6);
	if ((e = esp_now_add_peer(&p)) != ESP_OK || (e = esp_now_register_recv_cb(recv_cb)) != ESP_OK ||
	    (e = esp_now_register_send_cb(send_cb)) != ESP_OK) {
		esp_now_deinit();
		return e;
	}
	if (cfg.rate != WIFI_PHY_RATE_1M_L) {
		if ((e = esp_wifi_config_espnow_rate(WIFI_IF_STA, cfg.rate)) != ESP_OK) {
			esp_now_deinit();
			return e;
		}
		rate_changed = true;
	}
	run = alive = true;
	xTaskNotifyGive(worker);
	ESP_LOGI(TAG, "role %d with " MACSTR " on channel %u", cfg.role, MAC2STR(cfg.peer), channel);
	return ESP_OK;
}

void @NAME@_espnow_stop(void) {
	run = false;
	for (int i = 0; i < 300 && alive; i++) vTaskDelay(pdMS_TO_TICKS(10));
	if (alive) ESP_LOGE(TAG, "the task didn't stop in 3 s");
}

bool @NAME@_espnow_running(void) { return alive; }

size_t @NAME@_espnow_result(char *out, size_t cap) {
	static const char *const ROLES[] = {"echo", "ping", "flood"};
	if (!tx || !out || cap < 2) return 0;
	int n = snprintf(out, cap, "{\"role\":\"%s\",\"channel\":%u,\"queue_full\":%u,\"other\":%u,\"tx\":", ROLES[cfg.role],
			 channel, (unsigned) q_full, (unsigned) other);
	if (n < 0 || (size_t) n >= cap) goto full;
	size_t k = @NAME@_tx_json(tx, out + n, cap - n);
	if (!k) goto full;
	n += k;
	if (cfg.role == @UNAME@_ROLE_ECHO) {
		if ((size_t) n + 7 >= cap) goto full;
		n += snprintf(out + n, cap - n, ",\"rx\":");
		if (!(k = @NAME@_rx_json(rx, out + n, cap - n))) goto full;
		n += k;
	}
	if ((size_t) n + 2 > cap) goto full;
	out[n++] = '}';
	out[n] = 0;
	return n;
full:
	out[0] = 0;
	return 0;
}
