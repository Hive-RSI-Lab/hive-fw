/*
 * @NAME@ over BLE GATT: one board echoes (the GATT server), the other pings
 * it (round trip, loss, clock offset and drift) or floods it (throughput;
 * the echo end counts what arrived).
 *
 * The echo board advertises its mark (@NAME@_bleutil.h) only during a run
 * and only keeps a link from the board named in peer: the peer's BLE
 * address is worked out from its Wi-Fi station MAC (@NAME@_peer_ble_addr),
 * anything else that connects is dropped at once and counted (rejected).
 * The ping and flood boards scan for the peer's mark, connect, ask for a
 * 247-byte MTU, find the service by discovery, turn notifications on and
 * run. Frames are the same as espnow-link's (@NAME@.h), written without
 * response one way and notified the other. No pairing, no bonding, no
 * link encryption: each frame carries @NAME@.h's tag under the run's key
 * instead, so a stranger's forged or seq-jumped frames count as bad and
 * move nothing but bad (a replay within a run is a dup: see @NAME@.h). Pairing would not do: Just Works (the only mode two
 * headless boards have) is open to a man in the middle, and a bond is per
 * pair of boards where the key is per run. The frames aren't secret:
 * anything that must be needs LE Secure Connections or encryption at the
 * application layer.
 *
 * Bluetooth must be up first: the controller enabled in a mode with BLE and
 * Bluedroid enabled (@NAME@_ble_bt_init does both when nothing else has).
 * Bluedroid has one BLE GAP callback, one GATT server callback and one GATT
 * client callback: the first start registers this component's, so an image
 * with its own BLE code must merge them (Classic BT, A2DP's GAP among them,
 * has callbacks of its own and is not touched). The GATT apps, the service
 * and the worker task are made once and kept, so a late callback never
 * finds freed memory.
 *
 * Tested on the ESP32 only (on v5 other chips need
 * CONFIG_BT_BLE_42_FEATURES_SUPPORTED for the legacy advertising and scan
 * calls). Bluedroid must stay up once a run has started: the GATT apps are
 * registered once and their ifs cached, so a Bluedroid deinit and init
 * (or a controller restart) is not supported.
 *
 * Measured on two ESP32-WROVER-E boards beside Wi-Fi STA and the A2DP sink
 * (see TEMPLATE.md).
 *
 * start and stop are not safe to call concurrently: callers serialise them
 * (one task, or a lock around both).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "@NAME@_bleutil.h"
#include "esp_err.h"

#include "@NAME@.h" /* @UNAME@_KEY */

typedef enum { @UNAME@_BLE_ECHO = 0, @UNAME@_BLE_PING = 1, @UNAME@_BLE_FLOOD = 2 } @NAME@_ble_role_t;

typedef struct {
	@NAME@_ble_role_t role;
	uint8_t peer[6];   /* the other board's Wi-Fi station MAC (@NAME@_parse_mac) */
	uint32_t rate_hz;  /* ping: 1-100 a second (0: 20) */
	uint32_t len;      /* frame bytes with the tag, @UNAME@_MIN..@UNAME@_BLE_PAYLOAD (0: 64, flood the most);
	                    * a link whose MTU won't carry it ends the run ("fail") */
	uint32_t count;    /* ping and flood: frames sent, then the run ends itself (0: until
	                    * stopped). Echo runs until stopped */
	float interval_ms; /* ping and flood: the connection interval to ask for, 7.5-100
	                    * (0: the stack's, 15 ms in Bluedroid) */
	uint32_t scan_s;   /* ping and flood: how long to look for the peer, 3-60 (0: 20) */
	/* the run ended by itself (count reached, link lost or failed). Called on
	 * the worker task after running() turned false: don't call start or stop
	 * from it, and don't write flash or NVS (the worker's stack may be in
	 * PSRAM): post to a task of your own for anything more than a flag */
	void (*on_end)(bool ok);
	uint8_t key[@UNAME@_KEY]; /* the run's frame key, the same at both ends: fresh for each run
	                           * and from the Hive's provisioning (or whoever starts the run),
	                           * never a constant in the code. All zero is refused. start keeps
	                           * it only as the HMAC states and wipes them at the run's end;
	                           * the caller wipes its own copy (@NAME@_wipe) */
} @NAME@_ble_cfg_t;

/* the controller (BLE only or dual mode, as the sdkconfig has it) and
 * Bluedroid, each brought up only if it isn't already */
esp_err_t @NAME@_ble_bt_init(void);

/* ESP_ERR_INVALID_ARG: a bad cfg; ESP_ERR_INVALID_STATE: Bluetooth isn't up,
 * a run is going (or still stopping), or the last run's link or connect is
 * still up (stop waited 2 s for it; try again); ESP_ERR_NO_MEM;
 * ESP_ERR_TIMEOUT: a GATT app didn't register; ESP_FAIL: the stack refused
 * the advertising data. Not safe against a concurrent start or stop. */
esp_err_t @NAME@_ble_start(const @NAME@_ble_cfg_t *cfg);
/* stops scanning or advertising, closes the link (waits up to 4 s). A
 * connect still pending is cancelled on IDF v5.5.6 and later; older IDFs
 * can't, so it runs out at CONFIG_BT_BLE_ESTAB_LINK_CONN_TOUT (30 s) and
 * start returns ESP_ERR_INVALID_STATE until then */
void @NAME@_ble_stop(void);
bool @NAME@_ble_running(void);
/* {"role","ok","phase",...,"conn":{...},"heap":{...},"tx":{...}} (echo adds
 * "rx"): the length, or 0 when cap is too small (1536 is plenty) */
size_t @NAME@_ble_result(char *out, size_t cap);
