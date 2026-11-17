/*
 * @NAME@ over BLE, the parts that need no radio (plain C, host-tested):
 * the advertisement that marks a board, the rule that turns the peer's
 * Wi-Fi MAC into its BLE address, the connection interval in the
 * controller's 1.25 ms units and the largest frame a link's MTU carries.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* the mark: company 0xffff (none: for tests and labs, never a product),
 * "hl" and the advertising board's Wi-Fi station MAC */
#define @UNAME@_MARK_LEN 10
/* flags (3 bytes) and the mark as manufacturer data (2 + 10): 15 of the 31
 * bytes a legacy advertisement holds */
#define @UNAME@_ADV_LEN 15
/* the MTU both ends ask for, and the largest value a write or a
 * notification then carries (the ATT header takes 3) */
#define @UNAME@_BLE_MTU 247
#define @UNAME@_BLE_PAYLOAD (@UNAME@_BLE_MTU - 3)

/* the advertisement for a board whose Wi-Fi MAC is mac: the length
 * (@UNAME@_ADV_LEN), or 0 when cap is too small */
size_t @NAME@_adv_build(const uint8_t mac[6], uint8_t *out, size_t cap);

/* does the advertisement (AD structures: length, type, data) carry the mark
 * of the board whose Wi-Fi MAC is mac? A structure running past n ends the
 * walk (false unless the mark came before it). */
bool @NAME@_adv_has_mark(const uint8_t *adv, size_t n, const uint8_t mac[6]);

/* The peer's BLE (public) address from its Wi-Fi station MAC, by the
 * offset this board's own two addresses have. On the ESP32 the BT address
 * is the base MAC + 2 and the station MAC the base MAC, and IDF adds that
 * offset to the last byte only, with no carry (esp_read_mac): so when this
 * board's two addresses share their first 5 bytes, the peer's last byte
 * gets the same offset, mod 256, and the rest is copied. Otherwise (this
 * board's addresses don't follow that rule) the offset is added as a 48-bit
 * number. Either way it holds only for boards whose MACs come from the
 * eFuse base MAC: give a board with a custom MAC its BLE address some other
 * way. */
void @NAME@_peer_ble_addr(const uint8_t peer_wifi[6], const uint8_t self_wifi[6], const uint8_t self_bt[6],
			  uint8_t out[6]);

/* a connection interval in ms as the controller's 1.25 ms units, rounded
 * to the nearest: 7.5-100 ms gives 6-80, anything else 0 (the controller's
 * full range is 7.5 ms to 4 s; past 100 ms a round trip takes most of a
 * second) */
uint16_t @NAME@_interval_units(float ms);

/* the largest frame a link with this MTU carries (MTU - 3, never above
 * @UNAME@_BLE_PAYLOAD); 0 below the 23-byte minimum */
uint16_t @NAME@_payload_for_mtu(uint16_t mtu);
