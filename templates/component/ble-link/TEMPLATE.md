# ble-link: a measured BLE GATT link between two boards, beside Wi-Fi and Classic BT

From the hi-fi Lite lab (two ESP32-WROVER-E boards, IDF v4.4.8, Bluedroid,
Wi-Fi station on the home AP and the A2DP sink up beside it).

- `@NAME@.c` / `include/@NAME@.h`: the link core, the same file as
  `espnow-link`'s (plain C, host-tested): the frames, the duplicate
  window, the echo end's counts, the pinger's round trips, NTP-style clock
  offset and drift, JSON, `@NAME@_take` (the callbacks' copy into the
  queue) and `@NAME@_receive` (what `handle()` does with a frame).
- `@NAME@_bleutil.c` / `include/@NAME@_bleutil.h`: the BLE parts that need
  no radio (plain C, host-tested). The advertisement that marks a board
  (company 0xffff, "hl" and its Wi-Fi MAC: for labs, never a product) and
  the walk that finds it; the peer's BLE address from its Wi-Fi MAC; a
  connection interval in the controller's 1.25 ms units; the largest frame
  a link's MTU carries.
- `@NAME@_ble.c` / `include/@NAME@_ble.h`: the radio side. Echo is the GATT
  server: it advertises its mark only during a run and keeps a link only
  from the peer it was given (anything else is dropped and counted,
  `rejected`). Ping and flood are the GATT client: scan for the peer's
  mark, connect, ask for a 247-byte MTU, find the service by discovery,
  turn notifications on, then write without response (ping at `rate_hz`,
  flood as fast as the stack takes it) and get the echo's answers as
  notifications. `count` ends ping and flood runs; echo runs until stopped.
  `on_end` says when a run ended by itself.
- `fuzz/fuzz_@NAME@.c` (into the project's fuzz dir): the receive side under
  `fw fuzz`. The echo knows its peer only by a BLE address, which is
  easy to forge, and only the frame tag stops a stranger's frames, so every value written or notified
  goes through `@NAME@_take` (as the callbacks queue it: too long counts
  as bad) and `@NAME@_receive`, the same calls `post()` and `handle()`
  make, with the counts, percentiles, drift and JSON checked after each;
  and the advertisement's mark walk on raw bytes. The counting is
  `espnow-link`'s, so its three regression inputs (`regress-forged`
  included) are in `seeds/` too.

Rules the lab found:

- BLE lost nothing where ESP-NOW lost 2.5–9% on the same boards at the same
  rates: the link layer acknowledges and retransmits inside the connection
  events, and the controller schedules them around Wi-Fi and Classic BT.
  Commands, sync beacons and settings need no retry layer on top.
- The round trip is about two connection intervals: median 18 ms at 7.5 ms,
  29 ms at 15 ms (Bluedroid's default), 58 ms at 30 ms, 88 ms at 50 ms; the
  worst in any run was 220 ms. The ping rate (20–100 Hz) made no difference.
  Pick the interval for the latency you need (`interval_ms`).
- About 470 kbit/s of payload with 244-byte writes at 15 ms, 457 at 30 ms,
  but only 395 at 7.5 ms: the shortest interval is not the fastest beside
  Wi-Fi and Classic BT.
- Clock drift from 30 s of GATT round trips is noise (−12 to +403 ppm): the
  best-of-each-second sample is quantised by the interval. Sync clocks over
  ESP-NOW, or from the controller's event timing over minutes.
- Bluetooth in dual mode costs about 12 KB of internal RAM. On the Lite the
  image failed its 12000-byte boot heap check until the static Wi-Fi RX
  buffers (8 → 5), Wi-Fi management short buffers (32 → 12) and the
  controller's Classic ACL links (→ 1) were cut, for a lab image only:
  fewer Wi-Fi buffers may cost throughput and the board's management link
  under load, which was not measured. Turning BLE pairing off saves nothing
  while Classic is on. The queue, the worker's stack and the statistics go
  in PSRAM when the board has it (the stack needs
  `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY`); a run dips internal RAM by
  about 3.3 KB more, Bluedroid's own link state and GATT client cache.
- BLE beside a live A2DP stream needs room for both links: an A2DP link
  holds about 6.3 KB of internal RAM on each board (source and sink alike)
  with free heap dipping about 1.5 KB lower, and a BLE link about 3.3 KB. So
  budget at least about 11 KB above your own run floor, beyond what the
  image needs to boot. A dual-mode Lite lab image with an A2DP source added
  booted with about 12 KB of internal RAM free, and the stream alone took
  both boards under 5 KB in 2.5–3.5 s, before any BLE. So whether BLE causes
  audio dropouts, or blocks an A2DP connect, is not measured yet; get the
  RAM first (statics to PSRAM, fewer stacks in internal RAM). Read the free
  internal heap with the stream up before starting a link. ESP-NOW receive
  did block A2DP connects (`espnow-link`'s rules).
- Bluedroid's GATT server and client add about 190 KB of flash and 13 KB of
  IRAM: the full Lite image no longer fitted its app slot. Budget for it,
  or try NimBLE (not tried here).
- The peer's BLE address comes from its Wi-Fi MAC: IDF makes the BT address
  base MAC + 2 and the station MAC the base MAC, adding the offset to the
  last byte only, with no carry. So `@NAME@_peer_ble_addr` applies this
  board's own offset to the peer's last byte. A board with a custom MAC
  needs its BLE address given some other way.
- Bluedroid has one BLE GAP, one GATT server and one GATT client callback:
  the first start registers this component's, so an image with BLE code of
  its own must merge them. Classic BT's callbacks (A2DP's GAP among them)
  are separate and untouched. The GATT apps, the service and the worker are
  made once and kept, so a late callback never finds freed memory.
- The lifecycle has races the first version got wrong, all fixed here:
  - a connect still pending at stop keeps Bluedroid trying into the next
    run: on IDF v5.5.6 and later stop cancels it
    (`esp_ble_gattc_cancel_open`) and waits for the answer; older IDFs
    (v4.4 among them) have no cancel, and a GAP disconnect can't stop a
    connect that has no link yet, so it runs out at
    `CONFIG_BT_BLE_ESTAB_LINK_CONN_TOUT` (30 s), start refuses until then,
    and a late connect is closed at once;
  - start refuses while the last run's link or connect is still up;
  - an advertising start that completes after stop is stopped again;
  - a rejected connection stops the advertising, so the echo advertises
    again when it drops, or the real peer can't get in;
  - `on_end` is called after `running()` turns false, on the worker task
    (its stack may be in PSRAM: no flash or NVS writes there).
- Tested on the ESP32 only. On IDF v5 the other chips need
  `CONFIG_BT_BLE_42_FEATURES_SUPPORTED` for the legacy advertising and scan
  calls. Bluedroid must stay up once a run has started: restarting it
  (deinit and init) leaves the cached GATT apps stale and is not supported.
- No pairing, bonding or link encryption: each frame carries
  `espnow-link`'s tag (HMAC-SHA256 of the header under the run's key,
  `cfg.key` from the project's provisioning), so a stranger's forged or
  seq-jumped frames count as bad and move nothing but bad (a replay
  within a run is a dup, and an echo needs a fresh start per run's key:
  see `@NAME@.h`). Pairing would not do:
  Just Works, the only mode two headless boards have, is open to a man in
  the middle, and a bond is per pair of boards where the key is per run.
  The frames aren't secret: anything that must be needs LE Secure
  Connections or encryption at the application layer.
- Data and method: hifi-lite-lab `lab/ble.md`.

`fw new component` adds the Bluetooth lines in template.json to the
project's `sdkconfig.defaults` (only the ones it doesn't set already).
For a project that was configured before, set them in menuconfig too (or
`idf.py menuconfig` and save): deleting `sdkconfig` instead drops every
menuconfig change that isn't in the defaults file. Lines in
`sdkconfig.defaults.<target>` files are not read or changed. A project on
NimBLE gets a warning: this component needs Bluedroid. The controller's mode stays the project's:
BLE only (IDF's default) or dual mode both work.

Use: `fw new component NAME --from ble-link`, call `NAME_ble_bt_init()`
(it brings up the controller and Bluedroid only if nothing has), then
`NAME_ble_start(&cfg)` on both boards (echo on one with the other's Wi-Fi
MAC as `peer`, ping or flood on the other, the same fresh `cfg.key` on both), `NAME_ble_result(buf, cap)` for
the JSON. `fw new check ble-link` builds it on every IDF in template.json.
