# espnow-link: a measured ESP-NOW link between two boards, beside Wi-Fi STA

From the hi-fi Lite lab (two ESP32-WROVER-E boards, IDF v4.4.8).

- `@NAME@.c` / `include/@NAME@.h`: plain C, host-tested. The frame both ends
  speak (magic, type, session, sequence, three timestamps, a payload pattern
  that catches corruption), a 1024-frame duplicate window, the sink's counts
  (loss, duplicates, reordering, throughput) and the pinger's statistics:
  a round-trip histogram (p50/p95/p99), and NTP-style clock offset with a
  least-squares drift in ppm from the best sample of each second (the base
  for synced playback across boards). Results come out as JSON.
- `@NAME@_espnow.c` / `include/@NAME@_espnow.h`: the radio side. Roles echo
  (answer pings, count data), ping (rate_hz frames a second) and flood (as
  fast as the radio takes them, `window` in flight).

Rules the lab found:

- ESP-NOW has the radio's one channel. Beside a station on an access
  point, both boards must be on the AP's channel; add the peer with
  channel 0. A board on another AP hears nothing.
- `esp_now_send` returns before the frame is on air; the send callback is
  the MAC-level ack. A flood that doesn't wait for the callbacks (a window)
  gets ESP_ERR_ESPNOW_NO_MEM, not more throughput.
- Wi-Fi modem sleep (the default with Bluetooth on) delays and drops
  frames to a sleeping peer; measure with the power save you will ship.
- Only frames from the configured peer are looked at; nothing is
  forwarded. Encryption (a PMK/LMK) is off in the template: turn it on for
  anything that carries commands.
- IDF 5.x moved `MACSTR` to `esp_mac.h` and `esp_random` to
  `esp_random.h`, changed the receive callback to take
  `esp_now_recv_info_t` (5.0) and the send callback to take
  `esp_now_send_info_t` (5.5); the template has both shapes under
  `ESP_IDF_VERSION` checks. On 5.5 `esp_wifi_config_espnow_rate` is
  deprecated (it still works; a warning, not an error). `fw new check
  espnow-link` builds it on every IDF in template.json.

Use: `fw new component NAME --from espnow-link`, call
`NAME_espnow_start(&cfg)` on both boards after `esp_wifi_start()` (echo on
one, ping or flood on the other), `NAME_espnow_result(buf, cap)` for the JSON.
