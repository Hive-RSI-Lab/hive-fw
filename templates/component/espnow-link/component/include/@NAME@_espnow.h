/*
 * @NAME@ over ESP-NOW: one board echoes, the other pings it (round trip,
 * loss, clock offset and drift) or floods it (throughput; the echo end
 * counts the loss).
 *
 * Wi-Fi must be started first (esp_wifi_start, station or AP). ESP-NOW
 * shares the one channel the radio is on: beside a station connected to an
 * access point, both boards must be on the AP's channel, so two boards on
 * the same AP just work and a board on another AP (or channel) hears
 * nothing. The peer is added with channel 0, "the channel we're on".
 *
 * Measured on two ESP32-WROVER-E boards beside Wi-Fi STA (see the README).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_wifi_types.h"

typedef enum { @UNAME@_ROLE_ECHO = 0, @UNAME@_ROLE_PING = 1, @UNAME@_ROLE_FLOOD = 2 } @NAME@_role_t;

typedef struct {
	@NAME@_role_t role;
	uint8_t peer[6];      /* the other board's station MAC (@NAME@_parse_mac) */
	uint32_t rate_hz;     /* ping: 1-100 a second (0: 20) */
	uint32_t len;         /* frame bytes, @UNAME@_HDR..@UNAME@_MAX (0: 64, flood 250) */
	uint32_t count;       /* frames, then the run ends itself (0: until stopped) */
	uint32_t window;      /* flood: frames in flight, 1-16 (0: 4) */
	wifi_phy_rate_t rate; /* 0 (WIFI_PHY_RATE_1M_L): ESP-NOW's default */
} @NAME@_espnow_cfg_t;

/* ESP_ERR_INVALID_ARG: a bad cfg; ESP_ERR_INVALID_STATE: running already */
esp_err_t @NAME@_espnow_start(const @NAME@_espnow_cfg_t *cfg);
/* stops and takes ESP-NOW down (waits up to 3 s for the task) */
void @NAME@_espnow_stop(void);
bool @NAME@_espnow_running(void);
/* {"role","channel","tx":{...},"rx":{...}}: the length, or 0 when cap is too small (1024 is plenty) */
size_t @NAME@_espnow_result(char *out, size_t cap);
