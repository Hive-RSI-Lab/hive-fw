# espnow-link: a measured ESP-NOW link between two boards, beside Wi-Fi STA

From the hi-fi Lite lab (two ESP32-WROVER-E boards, IDF v4.4.8).

- `@NAME@.c` / `include/@NAME@.h`: plain C, host-tested. The frame both ends
  speak: a 38-byte header (magic 2, version 1, type 1, the frame's total
  length 2, session 4, sequence 4, three 8-byte timestamps), a payload
  pattern derived from every byte of the sequence number, and an 8-byte
  tag: HMAC-SHA256 of the header under the run's key (`cfg.key`, 16 bytes
  from the project's provisioning, fresh for each run, never a constant in
  the code; an all-zero key is refused). A frame whose tag fails, whose
  length field isn't the bytes received (truncated or padded) or whose
  payload doesn't match is counted bad, not used. Frame version 3 (2 had
  no tag, 1 a 36-byte header without the length). A DATA frame of another
  session counts as `stray` while the current session was heard within the
  last second; after a second of silence the new session is adopted. Also
  a 1024-frame duplicate window, the sink's counts
  (loss, duplicates, reordering, throughput) and the pinger's statistics:
  a round-trip histogram (p50/p95/p99), and NTP-style clock offset with a
  least-squares drift in ppm from the best sample of each second, the
  still-open second included (the base for synced playback across boards).
  Results come out as JSON. `@NAME@_take` is the receive callback's copy
  into its queue item, and `@NAME@_receive` is what `handle()` does with a
  frame. `ble-link` uses the same file.
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
  reset the expected count (`seen` now refuses it). A third input,
  `regress-forged`, is a frame tagged under a key one bit off the run's:
  it must count as bad and move nothing else (the harness builds such
  frames for any input with flag bit 32 set).

Rules the lab found:

- ESP-NOW has the radio's one channel. Beside a station on an access
  point, both boards must be on the AP's channel; add the peer with
  channel 0. A board on another AP hears nothing.
- `esp_now_send` returns before the frame is on air; the send callback is
  the MAC-level ack. A flood that doesn't wait for the callbacks (a window)
  gets ESP_ERR_ESPNOW_NO_MEM, not more throughput.
- The receiver's Wi-Fi modem sleep is what loses frames, not Bluetooth.
  A board in modem sleep (`WIFI_PS_MIN_MODEM` or `MAX`, the default with
  Bluetooth on) has its radio off between beacons, and frames sent to it
  then fail after the MAC's retries: a window-16 flood to it lost 23–35%
  (about 75 frames a second, whatever the load). The lab isolated it on
  two Lites:
  with the receiver's Bluetooth controller disabled and modem sleep left
  on, the loss stayed the same; with modem sleep off as well
  (`WIFI_PS_NONE`) it lost nothing in 28000 frames and received 3.6 Mbit/s
  instead of about 0.4. Keeping Bluetooth up but quiet (no page or inquiry
  scans) with `esp_coex_preference_set(ESP_COEX_PREFER_WIFI)` didn't help.
  The sender's own modem sleep and Bluetooth didn't matter. So: a board
  that must receive ESP-NOW without loss needs `WIFI_PS_NONE`, which means
  its Bluetooth controller off for that time; a board that keeps Bluetooth
  on gets about a quarter to a third of a flood lost and must retry.
- With Bluetooth on, retry at the sender, up to 8 times; nothing else
  measured helps. Resend each frame whose send callback says it wasn't
  acked (same sequence number), up to 8 more times: the flood above then
  lost 0.09–0.44% in three runs (0.38–0.46% in two more with retries 50 ms
  apart), at 0.4–0.5 Mbit/s delivered and 1.30–1.46× the airtime. 4 retries
  still lost 1.3%, 6 lost 1.1%. A retried frame arrives out of order
  (396–995 frames in a 15–30 s run), so the receiver needs a reorder buffer by
  sequence number and must drop duplicates; spacing retries 50 ms apart
  works as well and adds latency. These didn't help: bursts timed to the
  receiver's DTIM wake (54–81% lost; likely, not tested, because the
  radio stays awake while traffic keeps it busy, not for a fixed time
  after the beacon), `WIFI_PS_MAX_MODEM` with `listen_interval` 1, 3 or
  10 (29–34%), and
  `ESP_COEX_PREFER_WIFI`. So pick one per link: Bluetooth on with 8
  retries for control, sync and data up to about 0.4 Mbit/s, or the
  receiver's Bluetooth controller off with `WIFI_PS_NONE` for 3.6–4.1
  Mbit/s and no loss. Switching costs about 0.65 s and drops a connected
  A2DP phone, so it is a mode, not a gap between audio frames. The A2DP
  stream's own dropouts beside the retries are not measured yet.
- Never `esp_wifi_set_ps(WIFI_PS_NONE)` while the Bluetooth controller is
  enabled: the coexistence code in the Wi-Fi blob logs "Should enable WiFi
  modem sleep when both WiFi and Bluetooth are enabled" and panics a few
  seconds later (IDF 4.4, seen on every try on the Lite). The template
  refuses `PS_NONE` while `esp_bt_controller_get_status()` says enabled.
  To get there at runtime: `esp_bluedroid_disable()`, wait (the Lite's
  sink waits 400 ms), `esp_bt_controller_disable()`, then
  `WIFI_PS_NONE`. To come back, put modem sleep on (`WIFI_PS_MIN_MODEM`)
  and check that `esp_wifi_set_ps()` returned `ESP_OK` before
  `esp_bt_controller_enable()` (if it failed, leave the controller off),
  then `esp_bluedroid_enable()`;
  profiles such as the A2DP sink have to be set up again (the Lite
  restarts). A connected phone is dropped.
- Only frames from the configured peer are looked at, but a MAC is easy to
  forge, so the tag is what keeps a stranger in radio range from moving the
  counts with a made-up frame or a jump in the sequence. Why a tag and not
  ESP-NOW's own encryption (LMK): an LMK frame that fails is dropped in the
  driver unseen, so a forger can't be counted; encrypted peers are capped
  (6 by default, 17 at most); Espressif documents no replay check for it;
  and it does nothing for `ble-link`, which shares these frames. The tag
  costs 8 bytes a frame (3.2% of a 250-byte frame) and two SHA-256 blocks
  whatever the length. In the lab (hifi-lite-lab `esp32-lab.md`) a third
  board forging the flooder's frames under its own key had all 539 counted
  bad and the loss unchanged, and the tagged image carried 2.8 Mbit/s
  against 3.8 untagged, an upper bound on the cost (the images differed in
  other ways too). The tag doesn't stop a replay within a run (a dup, see
  `@NAME@.h`) or jamming: 30 forged frames a second cut a flood ninefold.
  The frames aren't secret: anything that must be needs encryption too.
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
  nack after the retries); none was dropped inside the boards: the
  receiver was in modem sleep (above). Good for control and sync messages
  with an app-level retry (8 retries: under 0.5% lost, above); not for
  streaming audio. With the receiver's
  Bluetooth controller off and `WIFI_PS_NONE`, a 250-byte flood at 54M with
  a window of 16 lost nothing at 3.6 Mbit/s. Data and method: hifi-lite-lab
  `lab/espnow.md`.
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
one, ping or flood on the other, the same fresh `cfg.key` on both), `NAME_espnow_result(buf, cap)` for the JSON.
