/*
 * @NAME@: an app the Hive's bench can test and update over the network.
 *
 * Out of the box it joins Wi-Fi (default NVS, namespace "config", blobs
 * "ssid" and "password"), dials the Hive's bench endpoint once one is set
 * (`api remote`), answers the test API on the serial console
 * (`api <op> ["json"]`, a line `API {json}` back) and over the remote
 * channel, takes firmware updates and boots each new image on trial.
 *
 * Your app goes in app_start() below; add its health to selftest() and,
 * if a new image must prove it before it is kept, to trial_inputs().
 */
#include <stdio.h>
#include <string.h>

#include "esp_console.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#if ESP_IDF_VERSION_MAJOR >= 5
#include "esp_app_desc.h"
#include "esp_flash.h"
#include "esp_mac.h"
#else
#include "esp_spi_flash.h"
#endif

#include "rr_api.h"
#include "rr_boot.h"
#include "rr_relay.h"

static const char TAG[] = "@NAME@";

#ifdef CONFIG_RR_SELFTEST_FAIL
#define SELFTEST_FAIL 1
#else
#define SELFTEST_FAIL 0
#endif

static bool wifi_configured;

/* ------------------------------------------------------------------ your app */

static void app_start(void) {
	ESP_LOGI(TAG, "@NAME@ is running");
}

/* ------------------------------------------------------------------ Wi-Fi */

static bool wifi_load(char ssid[33], char pw[65]) {
	nvs_handle_t h;
	if (nvs_open("config", NVS_READONLY, &h) != ESP_OK) return false;
	size_t n = 32, m = 64;
	memset(ssid, 0, 33);
	memset(pw, 0, 65);
	bool ok = nvs_get_blob(h, "ssid", ssid, &n) == ESP_OK && ssid[0];
	if (ok && nvs_get_blob(h, "password", pw, &m) != ESP_OK) pw[0] = 0;
	nvs_close(h);
	return ok;
}

static void wifi_join(const char *ssid, const char *pw) {
	wifi_config_t c = {0};
	snprintf((char *) c.sta.ssid, sizeof(c.sta.ssid), "%s", ssid);
	snprintf((char *) c.sta.password, sizeof(c.sta.password), "%s", pw);
	esp_wifi_disconnect();
	esp_wifi_set_config(WIFI_IF_STA, &c);
	esp_wifi_connect();
	wifi_configured = true;
	ESP_LOGI(TAG, "joining %s", ssid);
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data) {
	if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED && wifi_configured) esp_wifi_connect();
	else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
		ip_event_got_ip_t *e = data;
		ESP_LOGI(TAG, "on the network: " IPSTR, IP2STR(&e->ip_info.ip));
	}
}

static void wifi_start(void) {
	esp_netif_init();
	esp_event_loop_create_default();
	esp_netif_create_default_wifi_sta();
	wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_wifi_init(&cfg));
	esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL);
	esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL);
	esp_wifi_set_storage(WIFI_STORAGE_RAM); /* ours is in "config" */
	esp_wifi_set_mode(WIFI_MODE_STA);
	ESP_ERROR_CHECK(esp_wifi_start());
	char ssid[33], pw[65];
	if (wifi_load(ssid, pw)) wifi_join(ssid, pw);
	else ESP_LOGW(TAG, "no Wi-Fi set: api wifi \"{\\\"ssid\\\":\\\"...\\\",\\\"password\\\":\\\"...\\\"}\"");
}

/* ------------------------------------------------------------------ the test API */

static void op_info(rr_info_t *i) {
#if ESP_IDF_VERSION_MAJOR >= 5
	const esp_app_desc_t *d = esp_app_get_description();
	uint32_t flash = 0;
	esp_flash_get_size(NULL, &flash);
#else
	const esp_app_desc_t *d = esp_ota_get_app_description();
	uint32_t flash = spi_flash_get_chip_size();
#endif
	uint8_t m[6];
	esp_read_mac(m, ESP_MAC_WIFI_STA);
	i->model = d->project_name;
	i->board = CONFIG_IDF_TARGET;
	i->fw = d->version;
	snprintf(i->mac, sizeof(i->mac), "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
	i->flash_mb = flash / (1024 * 1024);
}

static void op_selftest(rr_selftest_t *t) {
	char line[64];
	bool heap_ok = rr_heap_line(heap_caps_get_free_size(MALLOC_CAP_INTERNAL), rr_boot_heap(), line, sizeof(line));
	rr_selftest_add(t, "heap", heap_ok, "%s", line);

	bool net = rr_net_up(), relay_set, relay_up;
	rr_relay_up(&relay_set, &relay_up);
	if (!net) rr_selftest_add(t, "network", false, "%s", wifi_configured ? "Wi-Fi not joined" : "no Wi-Fi set");
	else if (relay_set && !relay_up) rr_selftest_add(t, "network", false, "Wi-Fi up, relay not reached");
	else rr_selftest_add(t, "network", true, "%s", relay_set ? "Wi-Fi and relay up" : "Wi-Fi up, no relay set");

	rr_ota_status_t o = {0};
	rr_relay_ota_status(&o);
	rr_selftest_add(t, "trial", strcmp(o.state, "unknown") != 0, "%s %s", o.slot, o.state);

	if (SELFTEST_FAIL) rr_selftest_add(t, "selftest_fail", false, "built with RR_SELFTEST_FAIL");
	/* your app's own checks: rr_selftest_add(t, "name", pass, "value") */
}

static void trial_inputs(rr_trial_t *t) {
	t->component_ok = true;
	t->wifi_configured = wifi_configured;
	t->wifi_ok = rr_net_up();
	t->heap_ok = heap_caps_get_free_size(MALLOC_CAP_INTERNAL) > RR_HEAP_MIN;
	t->forced_fail = SELFTEST_FAIL;
}

static void reboot_task(void *arg) {
	vTaskDelay(pdMS_TO_TICKS(500)); /* the reply goes out first */
	esp_restart();
}

static void op_reboot(void) { xTaskCreate(reboot_task, "reboot", 2048, NULL, 5, NULL); }

static int op_set_wifi(const char *ssid, const char *pw) {
	nvs_handle_t h;
	if (nvs_open("config", NVS_READWRITE, &h) != ESP_OK) return -1;
	esp_err_t e = nvs_set_blob(h, "ssid", ssid, strlen(ssid) + 1);
	if (e == ESP_OK) e = nvs_set_blob(h, "password", pw, strlen(pw) + 1);
	if (e == ESP_OK) e = nvs_commit(h);
	nvs_close(h);
	if (e != ESP_OK) return -1;
	wifi_join(ssid, pw);
	return 0;
}

static const rr_ops_t ops = {
	.info = op_info,
	.selftest = op_selftest,
	.reboot = op_reboot,
	.set_wifi = op_set_wifi,
	.remote_status = rr_relay_status,
	.remote_set = rr_relay_set,
	.ota = &rr_ota_session,
	.ota_status = rr_relay_ota_status,
};

/* api <op> ["json"]: GET without a body, POST with one; prints API {json},
 * with ,"status":N added when it isn't 200 */
static int cmd_api(int argc, char **argv) {
	if (argc < 2 || argc > 3) {
		printf("API {\"error\":\"usage: api <op> [json]\",\"status\":400}\n");
		return 1;
	}
	char path[96];
	snprintf(path, sizeof(path), "/api/v1/%s", argv[1]);
	const char *body = argc == 3 ? argv[2] : NULL;
	size_t cap = rr_api_reply_cap(path, body);
	char *out = malloc(cap);
	if (!out) {
		printf("API {\"error\":\"out of memory\",\"status\":500}\n");
		return 1;
	}
	int st = rr_api_handle(&ops, body ? "POST" : "GET", path, body, out, cap);
	size_t n = strlen(out);
	if (st != 200 && n && out[n - 1] == '}') {
		out[n - 1] = 0;
		printf("API %s,\"status\":%d}\n", out, st);
	} else {
		printf("API %s\n", out);
	}
	free(out);
	return st == 200 ? 0 : 1;
}

static void console_start(void) {
	esp_console_repl_t *repl = NULL;
	esp_console_repl_config_t rc = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
	rc.prompt = "@NAME@>";
	rc.max_cmdline_length = 1024; /* a Wi-Fi or relay setting in JSON */
	esp_console_dev_uart_config_t uc = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_console_new_repl_uart(&uc, &rc, &repl));
	const esp_console_cmd_t api = {
		.command = "api",
		.help = "the test API: api <op> [\"json\"], e.g. api info, api selftest \"{}\"",
		.func = cmd_api,
	};
	esp_console_cmd_register(&api);
	ESP_ERROR_CHECK(esp_console_start_repl(repl));
}

void app_main(void) {
	esp_err_t e = nvs_flash_init();
	if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
		ESP_ERROR_CHECK(nvs_flash_erase());
		e = nvs_flash_init();
	}
	ESP_ERROR_CHECK(e);
	wifi_start();
	rr_relay_start(&ops, trial_inputs);
	console_start();
	app_start();
}
