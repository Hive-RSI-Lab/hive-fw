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
- With Bluetooth in the image, never `esp_wifi_set_ps(WIFI_PS_NONE)`: the
  coexistence code in the Wi-Fi blob logs "Should enable WiFi modem sleep
  when both WiFi and Bluetooth are enabled" and panics a few seconds later
  (IDF 4.4, seen on every try on the Lite). Refuse it under
  `CONFIG_BT_ENABLED`; `WIFI_PS_MIN_MODEM` is the fastest you get.
- Only frames from the configured peer are looked at; nothing is
  forwarded. Encryption (a PMK/LMK) is off in the template: turn it on for
  anything that carries commands.
- Internal RAM is the scarce part, not flash. On the Lite (Wi-Fi + Bluetooth
  + audio) about 13 KB of internal RAM was left, and the first lab image
  could not start a run: a plain `xQueueCreate` of 32 frames (~8 KB) and a
  4 KB task stack both come from internal RAM in IDF 4.4, even with PSRAM
  malloc on. The template now puts the queue storage (`xQueueCreateStatic`),
  the worker's stack (`xTaskCreateStaticPinnedToCore`, needs
  `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY`) and the histogram in PSRAM
  when the board has it, and makes the worker once: it sleeps on a task
  notification between runs, so a static stack is never reused while a
  deleted task still holds it. A task with a PSRAM stack must not write
  flash (NVS); keep it to the radio. Check `heap_caps_get_free_size
  (MALLOC_CAP_INTERNAL)` before and after start on your board.
- IDF 5.x moved `MACSTR` to `esp_mac.h` and `esp_random` to
  `esp_random.h`, changed the receive callback to take
  `esp_now_recv_info_t` (5.0) and the send callback to take
  `esp_now_send_info_t` (5.5); the template has both shapes under
  `ESP_IDF_VERSION` checks. On 5.5 `esp_wifi_config_espnow_rate` is
  deprecated (it still works; a warning, not an error). `fw new check
  espnow-link` builds it on every IDF in template.json.
- What to expect (two Lites in one house, station on the home AP, Bluetooth
  on, `WIFI_PS_MIN_MODEM`): 64-byte ping at 1 Mbps, RTT median 3.7 ms,
  p99 30–75 ms, 3–9% of frames lost after the MAC's retries; a 250-byte
  flood peaks near 400 kbps received at 24M with a third of frames lost.
  Every lost frame was a MAC nack (the receiver's radio asleep), none was
  dropped inside the boards. Good for control and sync messages with an
  app-level retry; not for streaming audio. Data and method:
  hifi-lite-lab `lab/espnow.md`.
- A radio experiment can hang a board (one Lite stopped answering right
  after a run and stayed off the network). An unattended board needs
  `CONFIG_ESP_TASK_WDT_PANIC=y` so a stuck task restarts it, and a check
  that restarts it after long enough off its management link; record why
  it restarted (an RTC no-init marker) so the next self-test reports it.

Use: `fw new component NAME --from espnow-link`, call
`NAME_espnow_start(&cfg)` on both boards after `esp_wifi_start()` (echo on
one, ping or flood on the other), `NAME_espnow_result(buf, cap)` for the JSON.
