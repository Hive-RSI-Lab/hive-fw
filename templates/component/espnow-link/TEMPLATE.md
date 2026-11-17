# espnow-link: a measured ESP-NOW link between two boards, beside Wi-Fi STA

From the hi-fi Lite lab (two ESP32-WROVER-E boards, IDF v4.4.8).

- `@NAME@.c` / `include/@NAME@.h`: plain C, host-tested. The frame both ends
  speak: a 38-byte header (magic 2, version 1, type 1, the frame's total
  length 2, session 4, sequence 4, three 8-byte timestamps) and a payload
  pattern derived from every byte of the sequence number. A frame whose
  length field isn't the bytes received (truncated or padded) or whose
  payload doesn't match is counted bad, not used. Frame version 2 (version
  1 had a 36-byte header without the length). A DATA frame of another
  session counts as `stray` while the current session was heard within the
  last second; after a second of silence the new session is adopted. Also
  a 1024-frame duplicate window, the sink's counts
  (loss, duplicates, reordering, throughput) and the pinger's statistics:
  a round-trip histogram (p50/p95/p99), and NTP-style clock offset with a
  least-squares drift in ppm from the best sample of each second, the
  still-open second included (the base for synced playback across boards).
  Results come out as JSON.
- `@NAME@_espnow.c` / `include/@NAME@_espnow.h`: the radio side. Roles echo
  (answer pings, count data), ping (rate_hz frames a second) and flood (as
  fast as the radio takes them, `window` in flight; a send report missing
  for 1 s counts as lost and its slot is reused, `report_timeouts`).
  `count` ends ping and flood runs; echo runs until stopped. The stop puts
  the ESP-NOW rate back while ESP-NOW is still up (before
  `esp_now_deinit`), then the power save, but only if it is still the one
  the run set; the result reports the run's power save, not the live one.
  Start and stop must not run concurrently: callers serialise them.
- `fuzz/fuzz_@NAME@.c` (into the project's fuzz dir): the receive side under
  `fw fuzz`. Any board near enough can send a frame (a MAC is easy to
  forge), so frames go through decode, the sink's counts and the pinger's
  round trips by `@NAME@_receive`, the same call `handle()` makes, with
  the counts, percentiles, drift and JSON checked after each. It found
  two bugs, now fixed, with a regression input each in `seeds/`: a PONG with times far apart
  overflowed the 64-bit round-trip sum (`tx_pong` now uses only times an
  echo could have written, and a delay up to `@UNAME@_RTT_MAX_US`), and
  sequence number 0xFFFFFFFF wrapped the duplicate window's top to 0 and
  reset the expected count (`seen` now refuses it). Frames carry no
  authentication: anyone who knows the session can still bend the RTT
  and drift figures within those bounds.

Rules the lab found:

- ESP-NOW has the radio's one channel. Beside a station on an access
  point, both boards must be on the AP's channel; add the peer with
  channel 0. A board on another AP hears nothing.
- `esp_now_send` returns before the frame is on air; the send callback is
  the MAC-level ack. A flood that doesn't wait for the callbacks (a window)
  gets ESP_ERR_ESPNOW_NO_MEM, not more throughput.
- Wi-Fi modem sleep (the default with Bluetooth on) is a likely cause of
  delayed and lost frames to a sleeping peer, but the lab did not isolate
  it (see below); measure with the power save you will ship.
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
  `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY`), the histogram and the
  receive callback's item in PSRAM when the board has it (a 270-byte static
  item was enough to fail the Lite's 12000-byte heap check after a run;
  queue and semaphore control blocks stay internal: the ESP32's atomic
  compare-and-set doesn't work on PSRAM), and makes the worker once: it sleeps on a task
  notification between runs, so a static stack is never reused while a
  deleted task still holds it. A task with a PSRAM stack must not write
  flash (NVS); keep it to the radio. Check `heap_caps_get_free_size
  (MALLOC_CAP_INTERNAL)` before and after start on your board; ESP-NOW
  keeps about 230 bytes of internal RAM after its first init, even after
  `esp_now_deinit`.
- IDF 5.x moved `MACSTR` to `esp_mac.h` and `esp_random` to
  `esp_random.h`, changed the receive callback to take
  `esp_now_recv_info_t` (5.0) and the send callback to take
  `esp_now_send_info_t` (5.5); the template has both shapes under
  `ESP_IDF_VERSION` checks. On 5.5 `esp_wifi_config_espnow_rate` is
  deprecated (it still works; a warning, not an error). `fw new check
  espnow-link` builds it on every IDF in template.json.
- What to expect (two Lites in one house, station on the home AP, Bluetooth
  on, `WIFI_PS_MIN_MODEM`): 64-byte ping at 1 Mbps (light ping traffic),
  RTT median 3.7 ms, RTT p99 27.6–75.8 ms, 2.55–9.25% of frames lost after
  the MAC's retries; a 250-byte flood peaks near 400 kbps received at 24M
  with a third of frames lost. Every lost frame was a MAC-level failure (a
  nack after the retries); none was dropped inside the boards. Modem sleep
  on the receiver is a likely cause but was not isolated: a `ps none`
  comparison isn't possible with Bluetooth on. Good for control and sync
  messages with an app-level retry; not for streaming audio. Data and
  method: hifi-lite-lab `lab/espnow.md`.
- A radio experiment can hang a board (one Lite stopped answering right
  after a run and stayed off the network). An unattended board needs
  `CONFIG_ESP_TASK_WDT_PANIC=y` so a stuck task restarts it, and a check
  that restarts it after long enough off its management link; record why
  it restarted (an RTC no-init marker) so the next self-test reports it.
  Run those checks from an `esp_timer`, not from the task they watch and
  not under its lock: the task watchdog only sees subscribed tasks, so a
  task stuck holding a mutex can sit forever unless something outside it
  notices that its tick stopped (the Lite lab restarts after 3 min). Back
  that restart with the task watchdog (`esp_task_wdt_add(NULL)` just
  before `esp_restart()`): `esp_restart` runs the shutdown handlers first,
  `esp_wifi_stop` among them, and a hang inside the Wi-Fi driver would hang
  the restart too. Count `ESP_RST_TASK_WDT` and `ESP_RST_PANIC` as your own
  restart when the marker says so. Give
  the task watchdog room (15 s on the Lite) so a slow flash write
  doesn't trip it. The watched task's stack can go in PSRAM too, as long
  as it never writes flash: on the Lite, moving the lab tick's 3 KB stack
  there took free internal RAM from about 12 K to about 15 K.

Use: `fw new component NAME --from espnow-link`, call
`NAME_espnow_start(&cfg)` on both boards after `esp_wifi_start()` (echo on
one, ping or flood on the other), `NAME_espnow_result(buf, cap)` for the JSON.
