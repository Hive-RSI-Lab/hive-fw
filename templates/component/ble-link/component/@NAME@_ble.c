/* @NAME@ over BLE GATT: the radio side of @NAME@.h (see @NAME@_ble.h) */
#include "@NAME@_ble.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "@NAME@.h"
#include "esp_bt.h"
#include "esp_bt_device.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "esp_gatt_common_api.h"
#include "esp_gattc_api.h"
#include "esp_gatts_api.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
#include "esp_mac.h"
#include "esp_random.h"
#endif
#include "esp_system.h"
#include "esp_task.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "@NAME@";

static const char *const ROLE_NAMES[] = {"echo", "ping", "flood"};

/* how far the run got (the result names where a finished run was when it
 * ended). The echo is in advertise until its peer connects, ready while
 * the link is up, and advertise again when it drops */
typedef enum { PH_IDLE, PH_SCAN, PH_OPEN, PH_MTU, PH_FIND, PH_SUBSCRIBE, PH_READY, PH_ADVERTISE } phase_t;
static const char *const PHASE_NAMES[] = {"idle", "scan", "open", "mtu", "find", "subscribe", "ready", "advertise"};

typedef enum { EV_FRAME, EV_READY, EV_LOST, EV_FAIL } ev_t;

typedef struct {
	uint8_t ev;
	uint16_t len;
	int64_t t_us;
	uint8_t data[@UNAME@_BLE_PAYLOAD];
} rx_item_t;

#define QLEN 32
#define STACK 4096
#define APP_SERVER 0x4c41
#define APP_CLIENT 0x4c42

/* the service and its one characteristic (random 128-bit UUIDs,
 * little-endian): make your own for a product */
static const uint8_t SVC_UUID[16] = {0x6b, 0x1a, 0x5e, 0x2f, 0x91, 0x3c, 0x4d, 0x8a,
				     0xb2, 0x07, 0x5f, 0xe1, 0x00, 0x4c, 0x61, 0x62};
static const uint8_t CHR_UUID[16] = {0x6b, 0x1a, 0x5e, 0x2f, 0x91, 0x3c, 0x4d, 0x8a,
				     0xb2, 0x07, 0x5f, 0xe1, 0x01, 0x4c, 0x61, 0x62};

/* Made once and kept (see @NAME@_ble.h). The big parts (the queue's QLEN
 * frames, ~8 KB, the 4 KB stack, the ~4 KB histogram) go to PSRAM when the
 * board has it: beside Wi-Fi, Classic BT and audio, internal RAM is what
 * runs out. A task whose stack is in PSRAM must not write flash (NVS); this
 * one doesn't. */
static QueueHandle_t q;
static StaticQueue_t q_ctl;
static SemaphoreHandle_t reg_sem, space_sem, closed_sem;
static TaskHandle_t worker;
static StaticTask_t worker_tcb;
static @NAME@_tx_t *tx;
static @NAME@_rx_t *rx;
static rx_item_t *cb_it; /* the callbacks' item: they all run on Bluedroid's task, one at a time */
static @NAME@_key_t *key; /* the run's frame key: never logged or reported, wiped as the run ends */
static bool cbs_registered;
static esp_gatt_if_t gatts_if = ESP_GATT_IF_NONE, gattc_if = ESP_GATT_IF_NONE;
static uint16_t h_svc, h_val, h_cccd; /* the server's handles */

static volatile bool run, alive, connected, notify_on, congested, run_ok;
static volatile bool opening; /* a connect asked for and not yet answered (OPEN_EVT) */
static volatile phase_t phase, phase_end; /* now; where the last run ended */
static @NAME@_ble_cfg_t cfg;
static uint16_t iv_units; /* cfg.interval_ms in 1.25 ms units (0: the stack's) */
static uint8_t peer_bt[6], self[6], self_bt[6], remote[6];
static uint16_t conn_id, c_start, c_end, c_val, c_cccd; /* the client's handles */
static volatile uint16_t mtu = 23, mtu_link; /* now; the highest the run's links reached */
static volatile uint16_t ci_units, ci_latency, ci_timeout; /* interval (1.25 ms), latency, timeout (10 ms) */
static char fail[48];
static uint32_t heap_start, heap_min;
static int64_t t_link_us, t_start_us;
static volatile uint32_t q_full, other, rejected, seen_adv, connects, disconnects, congest_events, waits;
static volatile int last_disc_reason;

static uint32_t heap_now(void) { return heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT); }

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

/* on Bluedroid's task */
static void post(ev_t ev, const uint8_t *data, size_t n) {
	rx_item_t *it = cb_it;
	it->ev = ev;
	it->t_us = esp_timer_get_time();
	it->len = (uint16_t) @NAME@_take(it->data, sizeof(it->data), data, n); /* too long: 0, which decodes as bad */
	if (xQueueSend(q, it, 0) != pdTRUE) q_full++;
}

static void failed(const char *why) {
	if (!run) return; /* a late answer to a run already over */
	if (!fail[0]) strlcpy(fail, why, sizeof(fail));
	post(EV_FAIL, NULL, 0);
}

/* ------------------------------------------------------------- advertising */

static esp_ble_adv_params_t adv_params = {
	.adv_int_min = 0x40, /* 40 ms */
	.adv_int_max = 0x60, /* 60 ms */
	.adv_type = ADV_TYPE_IND,
	.own_addr_type = BLE_ADDR_TYPE_PUBLIC,
	.channel_map = ADV_CHNL_ALL,
	.adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
};

static bool advertise(void) {
	/* the flags and the mark: the client finds the service by discovery
	 * (a 128-bit UUID would take 18 more of the 31 bytes) */
	uint8_t raw[@UNAME@_ADV_LEN];
	size_t n = @NAME@_adv_build(self, raw, sizeof(raw));
	/* advertising starts once the data is set (gap_cb) */
	return n && esp_ble_gap_config_adv_data_raw(raw, n) == ESP_OK;
}

static void gap_cb(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *p) {
	switch (event) {
	case ESP_GAP_BLE_ADV_DATA_RAW_SET_COMPLETE_EVT:
		if (run && cfg.role == @UNAME@_BLE_ECHO && !connected) esp_ble_gap_start_advertising(&adv_params);
		break;
	case ESP_GAP_BLE_ADV_START_COMPLETE_EVT:
		if (p->adv_start_cmpl.status != ESP_BT_STATUS_SUCCESS) failed("advertising didn't start");
		/* a start queued just before teardown's stop: stop it again */
		else if (!run || cfg.role != @UNAME@_BLE_ECHO || connected) esp_ble_gap_stop_advertising();
		break;
	case ESP_GAP_BLE_SCAN_PARAM_SET_COMPLETE_EVT:
		if (run && phase == PH_SCAN) esp_ble_gap_start_scanning(cfg.scan_s);
		break;
	case ESP_GAP_BLE_SCAN_RESULT_EVT:
		if (!run || phase != PH_SCAN) break;
		if (p->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_RES_EVT) {
			seen_adv++;
			if (!@NAME@_adv_has_mark(p->scan_rst.ble_adv, p->scan_rst.adv_data_len, cfg.peer)) break;
			phase = PH_OPEN;
			esp_ble_gap_stop_scanning();
			memcpy(remote, p->scan_rst.bda, 6);
			opening = true;
			if (esp_ble_gattc_open(gattc_if, p->scan_rst.bda, p->scan_rst.ble_addr_type, true) != ESP_OK) {
				opening = false;
				failed("connect refused");
			}
		} else if (p->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_CMPL_EVT)
			failed("the peer wasn't advertising");
		break;
	case ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT:
		if (p->update_conn_params.status == ESP_BT_STATUS_SUCCESS && connected &&
		    !memcmp(p->update_conn_params.bda, remote, 6)) {
			ci_units = p->update_conn_params.conn_int;
			ci_latency = p->update_conn_params.latency;
			ci_timeout = p->update_conn_params.timeout;
		}
		break;
	default:
		break;
	}
}

/* ------------------------------------------------------------------ server */

enum { IDX_SVC, IDX_CHR, IDX_VAL, IDX_CCCD, IDX_N };
static const uint16_t UUID_PRI = ESP_GATT_UUID_PRI_SERVICE, UUID_DECL = ESP_GATT_UUID_CHAR_DECLARE,
		      UUID_CCCD = ESP_GATT_UUID_CHAR_CLIENT_CONFIG;
static const uint8_t CHR_PROP = ESP_GATT_CHAR_PROP_BIT_WRITE_NR | ESP_GATT_CHAR_PROP_BIT_WRITE | ESP_GATT_CHAR_PROP_BIT_NOTIFY;
static uint8_t cccd_val[2];
static const esp_gatts_attr_db_t DB[IDX_N] = {
	[IDX_SVC] = {{ESP_GATT_AUTO_RSP},
		     {ESP_UUID_LEN_16, (uint8_t *) &UUID_PRI, ESP_GATT_PERM_READ, 16, 16, (uint8_t *) SVC_UUID}},
	[IDX_CHR] = {{ESP_GATT_AUTO_RSP},
		     {ESP_UUID_LEN_16, (uint8_t *) &UUID_DECL, ESP_GATT_PERM_READ, 1, 1, (uint8_t *) &CHR_PROP}},
	[IDX_VAL] = {{ESP_GATT_AUTO_RSP},
		     {ESP_UUID_LEN_128, (uint8_t *) CHR_UUID, ESP_GATT_PERM_WRITE, @UNAME@_BLE_PAYLOAD, 0, NULL}},
	[IDX_CCCD] = {{ESP_GATT_AUTO_RSP},
		      {ESP_UUID_LEN_16, (uint8_t *) &UUID_CCCD, ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE, 2, 2, cccd_val}},
};

static void gatts_cb(esp_gatts_cb_event_t event, esp_gatt_if_t gif, esp_ble_gatts_cb_param_t *p) {
	switch (event) {
	case ESP_GATTS_REG_EVT:
		if (p->reg.app_id != APP_SERVER) break;
		if (p->reg.status != ESP_GATT_OK) {
			xSemaphoreGive(reg_sem);
			break;
		}
		gatts_if = gif;
		if (esp_ble_gatts_create_attr_tab(DB, gif, IDX_N, 0) != ESP_OK) xSemaphoreGive(reg_sem);
		break;
	case ESP_GATTS_CREAT_ATTR_TAB_EVT:
		if (p->add_attr_tab.status == ESP_GATT_OK && p->add_attr_tab.num_handle == IDX_N) {
			h_svc = p->add_attr_tab.handles[IDX_SVC];
			h_val = p->add_attr_tab.handles[IDX_VAL];
			h_cccd = p->add_attr_tab.handles[IDX_CCCD];
			esp_ble_gatts_start_service(h_svc);
		}
		xSemaphoreGive(reg_sem);
		break;
	/* Bluedroid tells every registered app about every link: on the client
	 * board the server sees the client's own link come and go, so the
	 * server's link events are the echo's alone */
	case ESP_GATTS_CONNECT_EVT:
		if (gif != gatts_if || cfg.role != @UNAME@_BLE_ECHO) break;
		if (!run || connected || memcmp(p->connect.remote_bda, peer_bt, 6)) {
			rejected++; /* remote stays the peer's */
			esp_ble_gap_disconnect(p->connect.remote_bda);
			break;
		}
		conn_id = p->connect.conn_id;
		memcpy(remote, p->connect.remote_bda, 6);
		ci_units = p->connect.conn_params.interval;
		ci_latency = p->connect.conn_params.latency;
		ci_timeout = p->connect.conn_params.timeout;
		connected = true;
		notify_on = false;
		connects++;
		t_link_us = esp_timer_get_time();
		phase = PH_READY;
		break;
	case ESP_GATTS_DISCONNECT_EVT:
		if (gif != gatts_if || cfg.role != @UNAME@_BLE_ECHO) break;
		if (!connected || p->disconnect.conn_id != conn_id) {
			/* a rejected one: its connect stopped the advertising, so start
			 * it again or the peer can't get in for the rest of the run */
			if (run && !connected && !advertise()) failed("advertising data refused");
			break;
		}
		connected = notify_on = false;
		disconnects++;
		last_disc_reason = p->disconnect.reason;
		mtu = 23;
		xSemaphoreGive(closed_sem);
		if (run) {
			phase = PH_ADVERTISE;
			if (!advertise()) failed("advertising data refused"); /* the client may come back */
		}
		break;
	case ESP_GATTS_MTU_EVT:
		if (gif == gatts_if && cfg.role == @UNAME@_BLE_ECHO && connected && p->mtu.conn_id == conn_id) {
			mtu = p->mtu.mtu;
			if (mtu > mtu_link) mtu_link = mtu;
		}
		break;
	case ESP_GATTS_WRITE_EVT:
		if (gif != gatts_if || cfg.role != @UNAME@_BLE_ECHO || !connected || p->write.conn_id != conn_id) break;
		if (p->write.is_prep) {
			other++; /* a long (prepared) write: frames fit one write, so not ours */
			break;
		}
		if (p->write.handle == h_cccd && p->write.len == 2)
			notify_on = p->write.value[0] & 1;
		else if (p->write.handle == h_val && run)
			post(EV_FRAME, p->write.value, p->write.len);
		break;
	case ESP_GATTS_CONGEST_EVT:
		if (gif != gatts_if || cfg.role != @UNAME@_BLE_ECHO) break;
		congested = p->congest.congested;
		if (congested) congest_events++;
		else xSemaphoreGive(space_sem);
		break;
	default:
		break;
	}
}

/* ------------------------------------------------------------------ client */

static void gattc_cb(esp_gattc_cb_event_t event, esp_gatt_if_t gif, esp_ble_gattc_cb_param_t *p) {
	if (event == ESP_GATTC_REG_EVT) {
		if (p->reg.app_id == APP_CLIENT) {
			if (p->reg.status == ESP_GATT_OK) gattc_if = gif;
			xSemaphoreGive(reg_sem);
		}
		return;
	}
	/* the client's events are the client roles' alone (see the server's) */
	if (gif != gattc_if || cfg.role == @UNAME@_BLE_ECHO) return;
	switch (event) {
	case ESP_GATTC_CONNECT_EVT:
		/* the link's first parameters (an update comes later as a GAP event) */
		if (run && phase == PH_OPEN && !memcmp(p->connect.remote_bda, remote, 6)) {
			ci_units = p->connect.conn_params.interval;
			ci_latency = p->connect.conn_params.latency;
			ci_timeout = p->connect.conn_params.timeout;
		}
		break;
	case ESP_GATTC_OPEN_EVT:
		opening = false;
		xSemaphoreGive(closed_sem); /* teardown may be waiting for this answer */
		if (!run || phase != PH_OPEN) { /* a late one: not wanted */
			if (p->open.status == ESP_GATT_OK) {
				/* start refuses until its DISCONNECT_EVT */
				conn_id = p->open.conn_id;
				connected = true;
				esp_ble_gattc_close(gif, p->open.conn_id);
			}
			break;
		}
		if (p->open.status != ESP_GATT_OK) {
			failed("connect failed");
			break;
		}
		conn_id = p->open.conn_id;
		connected = true;
		connects++;
		t_link_us = esp_timer_get_time();
		phase = PH_MTU;
		if (iv_units) {
			esp_ble_conn_update_params_t cp = {0};
			memcpy(cp.bda, p->open.remote_bda, 6);
			cp.min_int = cp.max_int = iv_units;
			cp.latency = 0;
			cp.timeout = 400; /* 4 s, in 10 ms units */
			esp_ble_gap_update_conn_params(&cp);
		}
		if (esp_ble_gattc_send_mtu_req(gif, conn_id) != ESP_OK) failed("MTU request refused");
		break;
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 6)
	case ESP_GATTC_CANCEL_OPEN_EVT:
		/* teardown's cancel of a pending connect: no OPEN_EVT is coming */
		if (p->cancel_open.status == ESP_GATT_OK && opening && !memcmp(p->cancel_open.remote_bda, remote, 6)) {
			opening = false;
			xSemaphoreGive(closed_sem);
		}
		break;
#endif
	case ESP_GATTC_CFG_MTU_EVT:
		if (!run || phase != PH_MTU || p->cfg_mtu.conn_id != conn_id) break;
		if (p->cfg_mtu.status == ESP_GATT_OK) mtu = p->cfg_mtu.mtu;
		if (mtu > mtu_link) mtu_link = mtu;
		/* a refused MTU leaves 23: short frames still run, a long len fails */
		phase = PH_FIND;
		c_start = c_end = 0;
		esp_bt_uuid_t u = {.len = ESP_UUID_LEN_128};
		memcpy(u.uuid.uuid128, SVC_UUID, 16);
		if (esp_ble_gattc_search_service(gif, conn_id, &u) != ESP_OK) failed("service search refused");
		break;
	case ESP_GATTC_SEARCH_RES_EVT:
		if (run && phase == PH_FIND && p->search_res.conn_id == conn_id &&
		    p->search_res.srvc_id.uuid.len == ESP_UUID_LEN_128 &&
		    !memcmp(p->search_res.srvc_id.uuid.uuid.uuid128, SVC_UUID, 16)) {
			c_start = p->search_res.start_handle;
			c_end = p->search_res.end_handle;
		}
		break;
	case ESP_GATTC_SEARCH_CMPL_EVT: {
		if (!run || phase != PH_FIND || p->search_cmpl.conn_id != conn_id) break;
		if (!c_start) {
			failed("no service on the peer");
			break;
		}
		esp_gattc_char_elem_t ch;
		uint16_t n = 1;
		esp_bt_uuid_t u = {.len = ESP_UUID_LEN_128};
		memcpy(u.uuid.uuid128, CHR_UUID, 16);
		if (esp_ble_gattc_get_char_by_uuid(gif, conn_id, c_start, c_end, u, &ch, &n) != ESP_GATT_OK || !n) {
			failed("no characteristic");
			break;
		}
		c_val = ch.char_handle;
		phase = PH_SUBSCRIBE;
		if (esp_ble_gattc_register_for_notify(gif, remote, c_val) != ESP_OK) failed("notify refused");
		break;
	}
	case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
		if (!run || phase != PH_SUBSCRIBE) break;
		esp_gattc_descr_elem_t d;
		uint16_t n = 1;
		esp_bt_uuid_t u = {.len = ESP_UUID_LEN_16, .uuid.uuid16 = ESP_GATT_UUID_CHAR_CLIENT_CONFIG};
		if (p->reg_for_notify.status != ESP_GATT_OK ||
		    esp_ble_gattc_get_descr_by_char_handle(gif, conn_id, c_val, u, &d, &n) != ESP_GATT_OK || !n) {
			failed("no notify descriptor");
			break;
		}
		c_cccd = d.handle;
		uint8_t on[2] = {1, 0};
		if (esp_ble_gattc_write_char_descr(gif, conn_id, c_cccd, 2, on, ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE) !=
		    ESP_OK)
			failed("notify write refused");
		break;
	}
	case ESP_GATTC_WRITE_DESCR_EVT:
		if (!run || phase != PH_SUBSCRIBE || p->write.conn_id != conn_id) break;
		if (p->write.status != ESP_GATT_OK) {
			failed("notify write failed");
			break;
		}
		phase = PH_READY;
		post(EV_READY, NULL, 0);
		break;
	case ESP_GATTC_NOTIFY_EVT:
		if (run && connected && p->notify.conn_id == conn_id && p->notify.handle == c_val)
			post(EV_FRAME, p->notify.value, p->notify.value_len);
		break;
	case ESP_GATTC_CONGEST_EVT:
		congested = p->congest.congested;
		if (congested) congest_events++;
		else xSemaphoreGive(space_sem);
		break;
	case ESP_GATTC_DISCONNECT_EVT:
		if (!connected || p->disconnect.conn_id != conn_id) break;
		connected = false;
		disconnects++;
		last_disc_reason = p->disconnect.reason;
		mtu = 23;
		xSemaphoreGive(closed_sem);
		if (run) {
			if (!fail[0]) snprintf(fail, sizeof(fail), "link lost (reason 0x%02x)", p->disconnect.reason);
			post(EV_LOST, NULL, 0);
		}
		break;
	default:
		break;
	}
}

/* ------------------------------------------------------------------ worker */

static bool send_frame(@NAME@_frame_t *f) {
	uint8_t buf[@UNAME@_MAX];
	int64_t t = esp_timer_get_time();
	if (f->type == @UNAME@_PING) f->t1 = t;
	else if (f->type == @UNAME@_PONG) f->t3 = t;
	size_t n = @NAME@_encode(key, f, buf, sizeof(buf));
	esp_err_t e = ESP_FAIL;
	if (n && connected) {
		if (cfg.role == @UNAME@_BLE_ECHO)
			e = notify_on ? esp_ble_gatts_send_indicate(gatts_if, conn_id, h_val, n, buf, false) : ESP_FAIL;
		else
			e = esp_ble_gattc_write_char(gattc_if, conn_id, c_val, n, buf, ESP_GATT_WRITE_TYPE_NO_RSP,
						     ESP_GATT_AUTH_REQ_NONE);
	}
	@NAME@_tx_sent(tx, n ? n : f->len, e == ESP_OK, t);
	return e == ESP_OK;
}

static void handle(const rx_item_t *it) {
	@NAME@_frame_t pong;
	switch (@NAME@_receive(key, rx, tx, cfg.role == @UNAME@_BLE_ECHO, cfg.role != @UNAME@_BLE_ECHO, it->data, it->len,
	                       it->t_us, &pong)) {
	case @UNAME@_GOT_REPLY:
		send_frame(&pong);
		break;
	case @UNAME@_GOT_OTHER:
		other++;
		break;
	default:
		break;
	}
}

/* room in the stack for one more write: not congested and a buffer free on
 * the link. Waits a little; false: none yet */
static bool room(void) {
	if (!congested && esp_ble_get_cur_sendable_packets_num(conn_id) > 0) return true;
	waits++;
	/* at least a tick: a spin here starves the idle task (and its watchdog) */
	xSemaphoreTake(space_sem, congested ? pdMS_TO_TICKS(20) + 1 : 1);
	return false;
}

static void teardown(void) {
	run = false;
	if (cfg.role == @UNAME@_BLE_ECHO) {
		esp_ble_gap_stop_advertising();
		if (connected) esp_ble_gap_disconnect(remote);
	} else {
		if (phase == PH_SCAN) esp_ble_gap_stop_scanning();
		if (connected) {
			esp_ble_gattc_close(gattc_if, conn_id);
			esp_ble_gap_disconnect(remote); /* the link too, if another app holds it */
		}
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 6)
		/* a connect still pending: cancel it, or Bluedroid keeps trying into
		 * the next run */
		else if (opening) {
			esp_ble_gattc_cancel_open_params_t cp = {.gattc_if = gattc_if};
			memcpy(cp.remote_bda, remote, 6);
			esp_ble_gattc_cancel_open(&cp);
		}
#endif
		/* older IDFs can't cancel one (a GAP disconnect needs a link): it stays
		 * pending until the stack's connect timeout
		 * (CONFIG_BT_BLE_ESTAB_LINK_CONN_TOUT, 30 s), start refuses meanwhile,
		 * and a late OPEN is closed */
	}
	/* the link down and any connect answered before the next start (2 s;
	 * start refuses while either is left) */
	int64_t until = esp_timer_get_time() + 2000000;
	while ((connected || opening) && esp_timer_get_time() < until) xSemaphoreTake(closed_sem, pdMS_TO_TICKS(100));
	phase_end = phase;
	phase = PH_IDLE;
	if (key) @NAME@_key_wipe(key); /* the run's key goes with the run */
}

/* the run; true when it ended by itself (on_end is due) */
static bool one_run(bool *ok_out) {
	bool ok = true, ready = cfg.role == @UNAME@_BLE_ECHO;
	rx_item_t it;
	uint32_t seq = 0;
	int64_t period = 1000000 / cfg.rate_hz, next = 0, done_at = 0;
	@NAME@_frame_t f = {.session = tx->session, .len = (uint16_t) cfg.len};
	while (run) {
		int64_t now = esp_timer_get_time();
		uint32_t h = heap_now();
		if (h < heap_min) heap_min = h;
		if (!ready && now - t_start_us > (int64_t) (cfg.scan_s + 15) * 1000000) {
			if (!fail[0])
				snprintf(fail, sizeof(fail), "no link in %u s (%s)", (unsigned) cfg.scan_s + 15, PHASE_NAMES[phase]);
			ok = false;
			break;
		}
		if (ready && connected && cfg.role != @UNAME@_BLE_ECHO && cfg.len > @NAME@_payload_for_mtu(mtu)) {
			if (!fail[0]) snprintf(fail, sizeof(fail), "len %u over the link's %u", (unsigned) cfg.len,
					       (unsigned) @NAME@_payload_for_mtu(mtu));
			ok = false;
			break;
		}
		if (ready && cfg.role == @UNAME@_BLE_FLOOD) {
			/* frames from the echo end (none expected) still get read */
			while (xQueueReceive(q, &it, 0) == pdTRUE) {
				if (it.ev == EV_FRAME) handle(&it);
				else if (it.ev == EV_LOST || it.ev == EV_FAIL) {
					ok = false;
					break;
				}
			}
			if (!ok) break;
			if (cfg.count && seq >= cfg.count) {
				if (!done_at) done_at = now + 500000; /* let the last writes out */
				if (now >= done_at) break;
				vTaskDelay(pdMS_TO_TICKS(20));
				continue;
			}
			if (!room()) continue;
			f.type = @UNAME@_DATA;
			f.seq = seq;
			if (send_frame(&f)) seq++;
			else vTaskDelay(1); /* refused: never spin */
			continue;
		}
		TickType_t wait = pdMS_TO_TICKS(100);
		if (ready && cfg.role == @UNAME@_BLE_PING && !done_at) {
			if (now >= next) {
				if (cfg.count && seq >= cfg.count)
					done_at = now + 1000000; /* a second for the last pongs */
				else {
					f.type = @UNAME@_PING;
					f.seq = seq++;
					f.t2 = f.t3 = 0;
					if (!congested) send_frame(&f);
					else @NAME@_tx_sent(tx, cfg.len, false, now); /* skipped: the link is backed up */
					next += period;
					if (next < now) next = now + period; /* fell behind: don't burst */
				}
				continue;
			}
			wait = (next - now) / 1000 / portTICK_PERIOD_MS;
			if (wait < 1) wait = 1; /* never spin: idle on this core feeds the watchdog */
		}
		if (done_at && now >= done_at) break;
		if (xQueueReceive(q, &it, wait) != pdTRUE) continue;
		if (it.ev == EV_FRAME) handle(&it);
		else if (it.ev == EV_READY && !ready) {
			ready = true;
			next = esp_timer_get_time();
		} else if (it.ev == EV_FAIL || (it.ev == EV_LOST && cfg.role != @UNAME@_BLE_ECHO)) {
			ok = false;
			break;
		}
	}
	bool ended_itself = run;
	run_ok = ok;
	teardown();
	ESP_LOGI(TAG, "%s stopped%s%s: sent %u, pongs %u, rx %u", ROLE_NAMES[cfg.role], fail[0] ? ": " : "", fail,
		 (unsigned) tx->sent, (unsigned) tx->pongs, (unsigned) rx->rx);
	*ok_out = ok;
	return ended_itself;
}

static void task_main(void *arg) {
	for (;;) {
		ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
		bool ok, ended = one_run(&ok);
		void (*cb)(bool) = cfg.on_end;
		alive = false; /* first: on_end may look at running() or start again elsewhere */
		if (ended && cb) cb(ok);
	}
}

/* ------------------------------------------------------------------- setup */

esp_err_t @NAME@_ble_bt_init(void) {
	esp_err_t e;
#if CONFIG_IDF_TARGET_ESP32 && CONFIG_BTDM_CTRL_MODE_BTDM
	const esp_bt_mode_t mode = ESP_BT_MODE_BTDM; /* dual mode: Classic (A2DP) beside BLE */
#else
	const esp_bt_mode_t mode = ESP_BT_MODE_BLE;
#endif
	if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_IDLE) {
#if CONFIG_IDF_TARGET_ESP32 && CONFIG_BTDM_CTRL_MODE_BLE_ONLY
		esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT); /* ~30 KB back */
#endif
		esp_bt_controller_config_t c = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
		if ((e = esp_bt_controller_init(&c)) != ESP_OK) return e;
	}
	if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_INITED &&
	    (e = esp_bt_controller_enable(mode)) != ESP_OK)
		return e;
	if (esp_bluedroid_get_status() == ESP_BLUEDROID_STATUS_UNINITIALIZED && (e = esp_bluedroid_init()) != ESP_OK) return e;
	if (esp_bluedroid_get_status() == ESP_BLUEDROID_STATUS_INITIALIZED && (e = esp_bluedroid_enable()) != ESP_OK) return e;
	return ESP_OK;
}

/* once: the queue, the worker, the callbacks and both GATT apps */
static esp_err_t setup(void) {
	if (worker && gatts_if != ESP_GATT_IF_NONE && h_val && gattc_if != ESP_GATT_IF_NONE) return ESP_OK;
	if (esp_bt_controller_get_status() != ESP_BT_CONTROLLER_STATUS_ENABLED ||
	    esp_bluedroid_get_status() != ESP_BLUEDROID_STATUS_ENABLED) {
		ESP_LOGE(TAG, "Bluetooth isn't up (@NAME@_ble_bt_init)");
		return ESP_ERR_INVALID_STATE;
	}
	if (!worker) {
		uint8_t *qmem = big(QLEN * sizeof(rx_item_t));
		StackType_t *stack = stack_mem(STACK);
		if (!reg_sem) reg_sem = xSemaphoreCreateBinary();
		if (!space_sem) space_sem = xSemaphoreCreateBinary();
		if (!closed_sem) closed_sem = xSemaphoreCreateBinary();
		if (!tx) tx = big(sizeof(*tx)); /* ~4 KB: the round-trip histogram */
		if (!rx) rx = big(sizeof(*rx));
		if (!cb_it) cb_it = big(sizeof(*cb_it));
		if (!key) key = big(sizeof(*key));
		if (!qmem || !stack || !reg_sem || !space_sem || !closed_sem || !tx || !rx || !cb_it || !key) {
			free(qmem);
			free(stack);
			return ESP_ERR_NO_MEM;
		}
		if (!q) q = xQueueCreateStatic(QLEN, sizeof(rx_item_t), qmem, &q_ctl);
		else free(qmem); /* made on an earlier try whose task failed */
		worker = xTaskCreateStaticPinnedToCore(task_main, "@NAME@", STACK, NULL, ESP_TASK_PRIO_MIN + 5, stack,
						       &worker_tcb, 0);
		if (!worker) {
			free(stack);
			return ESP_ERR_NO_MEM;
		}
	}
	if (!cbs_registered) {
		esp_err_t e;
		if ((e = esp_ble_gap_register_callback(gap_cb)) != ESP_OK || (e = esp_ble_gatts_register_callback(gatts_cb)) != ESP_OK ||
		    (e = esp_ble_gattc_register_callback(gattc_cb)) != ESP_OK)
			return e;
		esp_ble_gatt_set_local_mtu(@UNAME@_BLE_MTU);
		cbs_registered = true;
	}
	if (gatts_if == ESP_GATT_IF_NONE || !h_val) {
		xSemaphoreTake(reg_sem, 0);
		/* registered but the table failed last time: make the table again */
		if (gatts_if == ESP_GATT_IF_NONE) esp_ble_gatts_app_register(APP_SERVER);
		else esp_ble_gatts_create_attr_tab(DB, gatts_if, IDX_N, 0);
		if (xSemaphoreTake(reg_sem, pdMS_TO_TICKS(3000)) != pdTRUE || gatts_if == ESP_GATT_IF_NONE || !h_val) {
			ESP_LOGE(TAG, "the GATT server didn't register");
			return ESP_ERR_TIMEOUT;
		}
	}
	if (gattc_if == ESP_GATT_IF_NONE) {
		xSemaphoreTake(reg_sem, 0);
		esp_ble_gattc_app_register(APP_CLIENT);
		if (xSemaphoreTake(reg_sem, pdMS_TO_TICKS(3000)) != pdTRUE || gattc_if == ESP_GATT_IF_NONE) {
			ESP_LOGE(TAG, "the GATT client didn't register");
			return ESP_ERR_TIMEOUT;
		}
	}
	return ESP_OK;
}

static esp_err_t start(@NAME@_ble_cfg_t *nc, const uint8_t k[@UNAME@_KEY], bool any);

esp_err_t @NAME@_ble_start(const @NAME@_ble_cfg_t *c) {
	if (!c) return ESP_ERR_INVALID_ARG;
	if (alive || connected || opening) return ESP_ERR_INVALID_STATE; /* a run, or the last one's link */
	@NAME@_ble_cfg_t n = *c;
	/* the key lives on only in key (as HMAC states): not in a cfg copy */
	uint8_t k[@UNAME@_KEY], any = 0;
	memcpy(k, n.key, sizeof(k));
	@NAME@_wipe(n.key, sizeof(n.key));
	for (size_t i = 0; i < sizeof(k); i++) any |= k[i];
	esp_err_t e = start(&n, k, any);
	@NAME@_wipe(k, sizeof(k));
	@NAME@_wipe(&n, sizeof(n));
	return e;
}

static esp_err_t start(@NAME@_ble_cfg_t *nc, const uint8_t k[@UNAME@_KEY], bool any) {
	@NAME@_ble_cfg_t n = *nc;
	if (!any) return ESP_ERR_INVALID_ARG; /* all zero: no key was given */
	if (!n.rate_hz) n.rate_hz = 20;
	if (!n.len) n.len = n.role == @UNAME@_BLE_FLOOD ? @UNAME@_BLE_PAYLOAD : 64;
	if (!n.scan_s) n.scan_s = 20;
	uint16_t units = n.interval_ms ? @NAME@_interval_units(n.interval_ms) : 0;
	if (n.role > @UNAME@_BLE_FLOOD || n.rate_hz > 100 || n.len < @UNAME@_MIN || n.len > @UNAME@_BLE_PAYLOAD ||
	    n.scan_s < 3 || n.scan_s > 60 || (n.interval_ms && !units))
		return ESP_ERR_INVALID_ARG;
	esp_err_t e = setup();
	if (e != ESP_OK) return e;
	cfg = n;
	@NAME@_key_init(key, k); /* the worker is idle */
	iv_units = units;

	esp_read_mac(self, ESP_MAC_WIFI_STA);
	memcpy(self_bt, esp_bt_dev_get_address(), 6);
	@NAME@_peer_ble_addr(cfg.peer, self, self_bt, peer_bt);

	heap_start = heap_min = heap_now();
	xQueueReset(q);
	xSemaphoreTake(space_sem, 0);
	xSemaphoreTake(closed_sem, 0);
	@NAME@_tx_init(tx, esp_random());
	@NAME@_rx_init(rx);
	q_full = other = rejected = seen_adv = connects = disconnects = congest_events = waits = 0;
	last_disc_reason = 0;
	ci_units = ci_latency = ci_timeout = 0;
	mtu = 23;
	mtu_link = 0;
	fail[0] = 0;
	congested = notify_on = connected = false;
	run_ok = true;
	phase_end = PH_IDLE;
	memset(remote, 0, 6);
	t_link_us = 0;
	t_start_us = esp_timer_get_time();
	run = alive = true;

	if (cfg.role == @UNAME@_BLE_ECHO) {
		phase = PH_ADVERTISE;
		if (!advertise()) {
			run = alive = false;
			phase = PH_IDLE;
			@NAME@_key_wipe(key);
			return ESP_FAIL;
		}
	} else {
		static esp_ble_scan_params_t sp = {
			.scan_type = BLE_SCAN_TYPE_PASSIVE,
			.own_addr_type = BLE_ADDR_TYPE_PUBLIC,
			.scan_filter_policy = BLE_SCAN_FILTER_ALLOW_ALL,
			.scan_interval = 0x50, /* 50 ms */
			.scan_window = 0x30,   /* 30 ms of it: Wi-Fi and Classic share the radio */
			.scan_duplicate = BLE_SCAN_DUPLICATE_DISABLE,
		};
		phase = PH_SCAN;
		if ((e = esp_ble_gap_set_scan_params(&sp)) != ESP_OK) {
			run = alive = false;
			phase = PH_IDLE;
			@NAME@_key_wipe(key);
			return e;
		}
	}
	xTaskNotifyGive(worker);
	ESP_LOGI(TAG, "%s with " MACSTR " (BLE " MACSTR ")", ROLE_NAMES[cfg.role], MAC2STR(cfg.peer), MAC2STR(peer_bt));
	return ESP_OK;
}

void @NAME@_ble_stop(void) {
	run = false;
	for (int i = 0; i < 400 && alive; i++) vTaskDelay(pdMS_TO_TICKS(10));
	if (alive) ESP_LOGE(TAG, "the task didn't stop in 4 s");
}

bool @NAME@_ble_running(void) { return alive; }

/* ------------------------------------------------------------------ result */

typedef struct {
	char *p;
	size_t cap, n;
	bool full;
} out_t;

static void put(out_t *o, const char *fmt, ...) {
	if (o->full) return;
	va_list ap;
	va_start(ap, fmt);
	int k = vsnprintf(o->p + o->n, o->cap - o->n, fmt, ap);
	va_end(ap);
	if (k < 0 || (size_t) k >= o->cap - o->n) o->full = true;
	else o->n += k;
}

static void put_mac(out_t *o, const char *key, const uint8_t *m) {
	put(o, "\"%s\":\"%02x:%02x:%02x:%02x:%02x:%02x\",", key, m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void put_json(out_t *o, size_t (*f)(const void *, char *, size_t), const void *arg) {
	if (o->full) return;
	size_t k = f(arg, o->p + o->n, o->cap - o->n);
	if (!k) o->full = true;
	else o->n += k;
}

static size_t tx_json(const void *t, char *out, size_t cap) { return @NAME@_tx_json(t, out, cap); }
static size_t rx_json(const void *r, char *out, size_t cap) { return @NAME@_rx_json(r, out, cap); }

size_t @NAME@_ble_result(char *out, size_t cap) {
	if (!tx || !out || cap < 2) return 0;
	out_t o = {out, cap, 0, false};
	put(&o, "{\"role\":\"%s\",\"ok\":%s,\"phase\":\"%s\",", ROLE_NAMES[cfg.role], run_ok ? "true" : "false",
	    PHASE_NAMES[alive ? phase : phase_end]);
	if (fail[0]) put(&o, "\"fail\":\"%s\",", fail); /* fixed texts and numbers: nothing to escape */
	put_mac(&o, "peer", cfg.peer);
	put_mac(&o, "peer_ble", peer_bt);
	put_mac(&o, "self_ble", self_bt);
	if (remote[0] | remote[1] | remote[2] | remote[3] | remote[4] | remote[5]) put_mac(&o, "remote", remote);
	put(&o, "\"connected\":%s,", connected ? "true" : "false");
	if (t_link_us) put(&o, "\"link_ms\":%lld,", (long long) ((t_link_us - t_start_us) / 1000));
	/* the link's MTU (not the 23 a disconnect leaves) and its parameters as
	 * last set: the interval in 1.25 ms units (x125 = ms x100), timeout in 10 ms */
	put(&o, "\"mtu\":%u,\"conn\":{\"interval_ms_x100\":%u,\"latency\":%u,\"timeout_ms\":%u", (unsigned) (mtu_link ? mtu_link : mtu),
	    (unsigned) ci_units * 125, (unsigned) ci_latency, (unsigned) ci_timeout * 10);
	if (iv_units) put(&o, ",\"asked_ms_x100\":%u", (unsigned) iv_units * 125);
	put(&o, "},\"len\":%u,", (unsigned) cfg.len);
	if (cfg.role == @UNAME@_BLE_PING) put(&o, "\"rate_hz\":%u,", (unsigned) cfg.rate_hz);
	if (cfg.role != @UNAME@_BLE_ECHO) put(&o, "\"adverts_seen\":%u,", (unsigned) seen_adv);
	put(&o, "\"connects\":%u,\"disconnects\":%u,", (unsigned) connects, (unsigned) disconnects);
	if (disconnects) put(&o, "\"last_disconnect_reason\":%d,", last_disc_reason);
	put(&o, "\"rejected\":%u,\"congested\":%u,", (unsigned) rejected, (unsigned) congest_events);
	if (cfg.role == @UNAME@_BLE_FLOOD) put(&o, "\"waits_for_room\":%u,", (unsigned) waits);
	put(&o, "\"queue_full\":%u,\"other\":%u,", (unsigned) q_full, (unsigned) other);
	/* internal RAM: at the start, the least during the run */
	put(&o, "\"heap\":{\"start\":%u,\"min\":%u},\"tx\":", (unsigned) heap_start, (unsigned) heap_min);
	put_json(&o, tx_json, tx);
	if (cfg.role == @UNAME@_BLE_ECHO) {
		put(&o, ",\"rx\":");
		put_json(&o, rx_json, rx);
	}
	put(&o, "}");
	if (o.full) {
		out[0] = 0;
		return 0;
	}
	return o.n;
}
