/*
 * The remote channel on the board (rr_relay.h): a WebSocket to the Hive's
 * bench endpoint (rr_ws.c), signed commands through rr_remote.c, replies,
 * status and events back. Also the update session's flash writes and the
 * trial boot that keeps or rolls back a new image.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sodium.h"
#if ESP_IDF_VERSION_MAJOR >= 5
#include "esp_app_desc.h"
#include "esp_mac.h"
#endif

#include "rr_json.h"
#include "rr_relay.h"
#include "rr_remote.h"
#include "rr_ws.h"
#include "rr_ws_proto.h"

static const char TAG[] = "rr_relay";

/* The Hive's public signing key(s), hex, comma-separated, from the build's
 * environment (HIFI_HIVE_PUBKEY; the component's CMakeLists.txt). Without
 * one the board still connects and reports, but takes no command. */
#ifndef RR_HIVE_PUBKEY
#define RR_HIVE_PUBKEY ""
#endif

#define SETTINGS_PART "settings"
#define SETTINGS_NS "config"
#define MSG_MAX (16 * 1024) /* an OTA chunk command is about 5.6 KB */

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 1, 0)
#define SNTP_MODE(m) esp_sntp_setoperatingmode(m)
#define SNTP_SERVER(i, s) esp_sntp_setservername(i, s)
#define SNTP_INIT() esp_sntp_init()
#define SNTP_POLL ESP_SNTP_OPMODE_POLL
#else
#define SNTP_MODE(m) sntp_setoperatingmode(m)
#define SNTP_SERVER(i, s) sntp_setservername(i, s)
#define SNTP_INIT() sntp_init()
#define SNTP_POLL SNTP_OPMODE_POLL
#endif

static const esp_app_desc_t *app_desc(void) {
#if ESP_IDF_VERSION_MAJOR >= 5
	return esp_app_get_description();
#else
	return esp_ota_get_app_description();
#endif
}

static const rr_ops_t *ops;
static rr_trial_inputs_fn trial_inputs;
static rr_remote_t rem;
static volatile bool connected;
static char url[128], user[48], pass[96], prefix[24] = "rr";
static char key[66]; /* the board's own key for the endpoint; never shown */
static bool ws_on, ws_bad; /* the link is running; its settings won't dial */
static QueueHandle_t inbox;
static bool rollback_reported;
static volatile bool settings_changed;
static volatile bool announce; /* the link just came up: send the status now */
static volatile uint32_t boot_heap;
static bool settings_ok; /* the settings partition opened */

rr_ota_t rr_ota_session;

/* ------------------------------------------------------------------ settings */

static void load_str(const char *k, const char *dflt, char *out, size_t n) {
	snprintf(out, n, "%s", dflt);
	nvs_handle_t h;
	if (!settings_ok || nvs_open_from_partition(SETTINGS_PART, SETTINGS_NS, NVS_READONLY, &h) != ESP_OK) return;
	size_t len = n;
	if (nvs_get_str(h, k, out, &len) != ESP_OK) snprintf(out, n, "%s", dflt);
	nvs_close(h);
}

static int save_str(const char *k, const char *v) {
	nvs_handle_t h;
	if (!settings_ok || nvs_open_from_partition(SETTINGS_PART, SETTINGS_NS, NVS_READWRITE, &h) != ESP_OK) return -1;
	esp_err_t e = nvs_set_str(h, k, v ? v : "");
	if (e == ESP_OK) e = nvs_commit(h);
	nvs_close(h);
	return e == ESP_OK ? 0 : -1;
}

int rr_relay_set(const char *u, const char *us, const char *pw, const char *pre, const char *k) {
	int err = 0;
	err |= save_str("hifi_relay_url", u);
	err |= save_str("hifi_relay_user", us);
	err |= save_str("hifi_relay_pass", pw);
	err |= save_str("hifi_relay_prefix", pre);
	err |= save_str("hifi_relay_key", k);
	if (err) return -1;
	settings_changed = true; /* the relay task reconnects with them */
	return 0;
}

static void load_settings(void) {
	load_str("hifi_relay_url", "", url, sizeof(url));
	load_str("hifi_relay_user", "", user, sizeof(user));
	load_str("hifi_relay_pass", "", pass, sizeof(pass));
	load_str("hifi_relay_prefix", "rr", prefix, sizeof(prefix));
	load_str("hifi_relay_key", "", key, sizeof(key));
}

bool rr_relay_has_key(void) { return rem.nkeys > 0; }

void rr_relay_up(bool *configured, bool *up) {
	*configured = url[0] != 0;
	*up = connected;
}

void rr_relay_status(rr_remote_status_t *r) {
	r->configured = url[0] != 0;
	r->connected = connected;
	snprintf(r->url, sizeof(r->url), "%s", url);
	snprintf(r->user, sizeof(r->user), "%s", user);
	snprintf(r->prefix, sizeof(r->prefix), "%s", prefix);
	snprintf(r->dev, sizeof(r->dev), "%s", rem.dev);
	r->has_key = key[0] != 0;
	r->keys = rem.nkeys;
	r->accepted = rem.accepted;
	r->rejected = rem.rejected;
}

uint32_t rr_boot_heap(void) { return boot_heap; }

bool rr_net_up(void) {
	esp_netif_t *n = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
	esp_netif_ip_info_t ip;
	return n && esp_netif_get_ip_info(n, &ip) == ESP_OK && ip.ip.addr != 0;
}

/* ------------------------------------------------------------------ sending */

/* t tags the message on the socket, which carries replies, status and events */
static void send_tagged(const char *t, const char *data) {
	if (!ws_on || !rr_ws_up()) return;
	size_t n = strlen(data), cap = n + 32;
	char *b = malloc(cap);
	if (!b) return;
	memcpy(b, data, n + 1);
	n = rr_ws_tag(b, n, cap, t);
	if (n) rr_ws_send(b, n);
	free(b);
}

static void publish_status(void) {
	rr_ota_status_t o = {0};
	rr_relay_ota_status(&o);
	char ip[16] = "";
	esp_netif_t *n = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
	esp_netif_ip_info_t ipi;
	if (n && esp_netif_get_ip_info(n, &ipi) == ESP_OK && ipi.ip.addr) snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ipi.ip));
	int rssi = 0;
	wifi_ap_record_t ap;
	if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
	const esp_app_desc_t *d = app_desc();

	char buf[320];
	hj_t j;
	hj_init(&j, buf, sizeof(buf));
	hj_obj(&j, NULL);
	hj_bool(&j, "online", true);
	hj_str(&j, "model", d->project_name);
	hj_str(&j, "fw", d->version);
	hj_str(&j, "ip", ip[0] ? ip : NULL);
	if (rssi) hj_int(&j, "rssi", rssi);
	else hj_null(&j, "rssi");
	hj_int(&j, "uptime_s", esp_timer_get_time() / 1000000);
	hj_obj(&j, "ota");
	hj_str(&j, "slot", o.slot);
	hj_str(&j, "state", o.state);
	hj_obj_end(&j);
	hj_obj_end(&j);
	send_tagged("status", buf);
}

static void publish_event(const char *event, const char *k1, const char *v1, const char *k2, const char *v2) {
	char buf[256];
	hj_t j;
	hj_init(&j, buf, sizeof(buf));
	hj_obj(&j, NULL);
	hj_str(&j, "event", event);
	hj_str(&j, "fw", app_desc()->version);
	if (k1) hj_str(&j, k1, v1);
	if (k2) hj_str(&j, k2, v2);
	hj_obj_end(&j);
	send_tagged("event", buf);
}

/* ------------------------------------------------------------------ commands */

static bool verify(const uint8_t pub[32], const uint8_t *msg, size_t n, const uint8_t sig[64]) {
	return crypto_sign_verify_detached(sig, msg, n, pub) == 0;
}

static int64_t clock_s(void) {
	time_t t = time(NULL);
	return t > 1700000000 ? (int64_t) t : 0; /* 0 until SNTP has set it */
}

typedef struct {
	char *data;
	size_t len;
} msg_t;

static void worker(void *arg) {
	msg_t m;
	for (;;) {
		if (xQueueReceive(inbox, &m, portMAX_DELAY) != pdTRUE) continue;
		size_t cap = rr_remote_reply_cap(m.data, m.len);
		char *out = malloc(cap);
		if (out) {
			int st = rr_remote_handle(&rem, ops, m.data, m.len, clock_s(), out, cap);
			send_tagged("reply", out);
			if (st != 200) ESP_LOGW(TAG, "command answered %d", st);
			free(out);
		}
		free(m.data);
	}
}

static void on_ws_msg(char *data, size_t len) {
	msg_t m = {.data = data, .len = len};
	if (xQueueSend(inbox, &m, 0) != pdTRUE) free(data);
}

static void on_ws_up(void) {
	connected = true;
	ESP_LOGI(TAG, "bench endpoint connected: %s as %s", url, rem.dev);
	announce = true; /* the relay task sends the status: it reads flash */
}

static void ws_start(void) {
	char h[RR_WS_HEADERS_MAX];
	if (rr_ws_headers(h, sizeof(h), user, pass, rem.dev, key) < 0) {
		ESP_LOGE(TAG, "%s needs the board's key from the Hive (and a whole service token, if any)", url);
		ws_bad = true;
		return;
	}
	ws_on = rr_ws_start(url, h, MSG_MAX, on_ws_msg, on_ws_up);
	memset(h, 0, sizeof(h));
	if (!ws_on) {
		ESP_LOGE(TAG, "couldn't start the link to %s", url);
		ws_bad = true;
	}
}

/* ------------------------------------------------------------------ the update's flash writes */

static esp_ota_handle_t ota_handle;
static const esp_partition_t *ota_part;

static int f_open(void *ctx, size_t size, const char **slot, const char **err) {
	ota_part = esp_ota_get_next_update_partition(NULL);
	if (!ota_part) return *err = "no app slot to write", -1;
	if (esp_ota_begin(ota_part, size, &ota_handle) != ESP_OK) return *err = "couldn't open the app slot", -1;
	*slot = ota_part->label;
	return 0;
}

static int f_write(void *ctx, const uint8_t *d, size_t n) {
	return esp_ota_write(ota_handle, d, n) == ESP_OK ? 0 : -1;
}

static int f_commit(void *ctx, const char **err) {
	if (esp_ota_end(ota_handle) != ESP_OK) return *err = "image didn't validate", -1;
	if (esp_ota_set_boot_partition(ota_part) != ESP_OK) return *err = "couldn't set the boot slot", -1;
	return 0;
}

static void f_abort(void *ctx) { esp_ota_abort(ota_handle); }

/* the console and the relay share the session */
static SemaphoreHandle_t ota_mux;
static void f_lock(void *ctx) { xSemaphoreTake(ota_mux, portMAX_DELAY); }
static void f_unlock(void *ctx) { xSemaphoreGive(ota_mux); }
static rr_ota_io_t ota_io = {.open = f_open, .write = f_write, .commit = f_commit, .abort = f_abort,
			     .lock = f_lock, .unlock = f_unlock};

void rr_relay_ota_status(rr_ota_status_t *o) {
	const esp_partition_t *run = esp_ota_get_running_partition();
	esp_ota_img_states_t st;
	o->slot = run->subtype == ESP_PARTITION_SUBTYPE_APP_FACTORY ? "recovery" : run->label;
	o->state = "unknown";
	if (esp_ota_get_state_partition(run, &st) == ESP_OK) {
		if (st == ESP_OTA_IMG_PENDING_VERIFY) o->state = "pending_verify";
		else if (st == ESP_OTA_IMG_VALID) o->state = "valid";
	} else if (run->subtype == ESP_PARTITION_SUBTYPE_APP_FACTORY) {
		o->state = "valid";
	}
	load_str("hifi_rb_from", "", o->last_from, sizeof(o->last_from));
	load_str("hifi_rb_why", "", o->last_reason, sizeof(o->last_reason));
}

/* ------------------------------------------------------------------ the relay task */

/* the slot that last ran well, for a recovery image that boots the app back */
static void note_good_slot(void) {
	const esp_partition_t *run = esp_ota_get_running_partition();
	if (run->subtype == ESP_PARTITION_SUBTYPE_APP_FACTORY) return;
	char saved[16];
	load_str("hifi_good_slot", "", saved, sizeof(saved));
	if (strcmp(saved, run->label)) save_str("hifi_good_slot", run->label);
}

static void relay_task(void *arg) {
	bool sntp = false, on_trial = false;
	uint32_t trial_start = 0, last_status = 0;
	const char *fw = app_desc()->version;
	esp_ota_img_states_t st;
	if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
		on_trial = true;
		ESP_LOGW(TAG, "firmware %s is on trial: it has %d s to prove itself", fw, RR_TRIAL_LIMIT_S);
	} else {
		note_good_slot();
	}
	char pending[8];
	load_str("hifi_rb_report", "0", pending, sizeof(pending));
	rollback_reported = strcmp(pending, "1") != 0;

	for (;;) {
		vTaskDelay(pdMS_TO_TICKS(2000));
		uint32_t now = esp_timer_get_time() / 1000000;
		bool up = rr_net_up();

		if (up && !sntp) {
			SNTP_MODE(SNTP_POLL);
			SNTP_SERVER(0, "pool.ntp.org");
			SNTP_INIT();
			sntp = true;
		}
		if (settings_changed) {
			settings_changed = false;
			if (ws_on) {
				rr_ws_stop();
				ws_on = false;
				connected = false;
			}
			ws_bad = false;
			load_settings();
		}
		if (ws_on) connected = rr_ws_up();
		if (up && url[0] && !ws_on && !ws_bad) {
			if (!strncmp(url, "wss://", 6)) ws_start();
			else {
				ESP_LOGE(TAG, "%s: only a wss:// endpoint is supported", url);
				ws_bad = true;
			}
		}

		if (connected && !rollback_reported) {
			rr_ota_status_t o = {0};
			rr_relay_ota_status(&o);
			publish_event("rolled_back", "from", o.last_from, "reason", o.last_reason);
			save_str("hifi_rb_report", "0");
			rollback_reported = true;
		}
		if (connected && (announce || now - last_status >= 60)) {
			announce = false;
			publish_status();
			last_status = now;
		}

		if (!boot_heap && rr_heap_boot_due(now, up, url[0] != 0, connected)) {
			boot_heap = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
			if (!boot_heap) boot_heap = 1;
			ESP_LOGI(TAG, "boot heap: %u B free", (unsigned) boot_heap);
		}

		if (on_trial) {
			if (!trial_start) trial_start = now ? now : 1;
			rr_trial_t t = {0};
			if (trial_inputs) trial_inputs(&t);
			t.no_key = rem.nkeys == 0;
			t.relay_configured = url[0] != 0;
			t.relay_ok = connected;
			char why[96];
			rr_trial_verdict_t v = rr_trial_judge(&t, now - trial_start, why, sizeof(why));
			if (v == RR_TRIAL_GOOD) {
				esp_ota_mark_app_valid_cancel_rollback();
				note_good_slot();
				on_trial = false;
				ESP_LOGI(TAG, "firmware %s kept: %s", fw, why);
				publish_event("ota_valid", "why", why, NULL, NULL);
				publish_status();
			} else if (v == RR_TRIAL_BAD) {
				ESP_LOGE(TAG, "firmware %s failed its trial: %s; rolling back", fw, why);
				save_str("hifi_rb_from", fw);
				save_str("hifi_rb_why", why);
				save_str("hifi_rb_report", "1");
				publish_event("ota_failed", "why", why, NULL, NULL);
				vTaskDelay(pdMS_TO_TICKS(500));
				esp_ota_mark_app_invalid_rollback_and_reboot();
			}
		}
	}
}

void rr_relay_start(const rr_ops_t *o, rr_trial_inputs_fn inputs) {
	ops = o;
	trial_inputs = inputs;
	esp_err_t e = nvs_flash_init_partition(SETTINGS_PART);
	/* never erase it: it holds the bench link and its key. Without it the
	 * trial fails and the bootloader goes back to the image that read it. */
	settings_ok = e == ESP_OK;
	if (!settings_ok) ESP_LOGE(TAG, "the settings partition didn't open: %s", esp_err_to_name(e));

	uint8_t mac[6];
	esp_read_mac(mac, ESP_MAC_WIFI_STA);
	rr_remote_dev_id(mac, rem.dev);
	if (sodium_init() < 0) ESP_LOGE(TAG, "libsodium didn't start");
	rem.verify = verify;
	if (!rr_remote_set_keys(&rem, RR_HIVE_PUBKEY)) ESP_LOGW(TAG, "no Hive key in this build: relay commands are refused");

	load_settings();

	const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
	ota_io.slot_size = next ? next->size : 0;
	ota_mux = xSemaphoreCreateMutex();
	if (!ota_mux) ota_io.lock = ota_io.unlock = NULL;
	rr_ota_init(&rr_ota_session, &ota_io);

	inbox = xQueueCreate(4, sizeof(msg_t));
	xTaskCreate(worker, "rr_cmd", 6144, NULL, ESP_TASK_PRIO_MIN + 2, NULL);
	xTaskCreate(relay_task, "rr_relay", 5120, NULL, ESP_TASK_PRIO_MIN + 1, NULL);
}
