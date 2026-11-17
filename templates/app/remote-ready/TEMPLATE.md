# remote-ready: an ESP32 app the Hive's bench can test, update and roll back over Wi-Fi

`fw new app NAME [DIR]` copies this into DIR (default ./NAME), with
`@NAME@` (the C name) in the project name, the console prompt and the log
tag. It is the remote side of the hi-fi Lite's firmware
(projects/fw-bench/remote.md), cut loose from the hi-fi: what a board needs
so that an agent can flash it, run its self-test and read its answers
through the bench, first over USB and then over Wi-Fi, with nobody at the
desk. Put your app in `app_start()` in `main/app_main.c`.

## What is in it

- `components/rr/`: the kit. The parts without ESP-IDF calls are plain C
  that `fw host-test` covers (`test/host/test_rr.c`, ASan and UBSan):
  - `rr_api`: the test API, `GET info`, `POST selftest`, `POST reboot`,
    `GET ota` and `POST ota/begin|chunk|finish|abort`, `POST wifi`,
    `GET|POST remote`, under `/api/v1/`;
  - `rr_remote`: the signed command channel (Ed25519 over
    `<line>\n<base64 signature>`, for this board's dev only, each id once);
  - `rr_ws_proto`, `rr_b64`, `rr_sha256`, `rr_json`: the WebSocket frames,
    base64, the image hash and a small JSON writer and reader;
  - `rr_ota`: the update session (sizes, order, hash) around `esp_ota_*`;
  - `rr_boot`: the trial-boot verdict and the boot-heap figure;
  - `rr_ws` and `rr_relay` (IDF side): the TLS WebSocket to the bench
    endpoint, the command worker, the flash writes, SNTP, and the trial.
- `main/app_main.c`: Wi-Fi from the default NVS, the `api` console
  command, the self-test, the trial inputs.
- `partitions.csv` (8 MB flash): `recovery` (factory), `ota_0`, `ota_1`,
  `nvs`, and `settings` (the link's settings, apart from Wi-Fi).
- `sdkconfig.defaults`: that table, bootloader rollback, the certificate
  bundle, a 6 KB main task stack.
- `bench.json`: the bench profile (below); `fw publish` reads it.
- `template.json`: the IDFs it builds on, v4.4.8 and v5.5.6. On IDF 5
  libsodium comes from the component registry (`idf_component.yml`, so the
  first build needs the network); IDF 4 has it built in.

## Provisioning

1. **The Hive key.** The board runs no remote command that the Hive's
   Ed25519 key didn't sign. Its public half goes in at build time from the
   environment, never in git:

       export HIFI_HIVE_PUBKEY=$(python3 ~/modules/dash/remote_bench.py pubkey)
       fw build

   The build says `Hive key: set`, or `Hive key: missing (HIFI_HIVE_PUBKEY)`:
   a build without it refuses every command, so it fails its self-test
   (`hive_key`) and its trial: sent as an update, it rolls back to the
   image that can still take one.
   Up to two keys, comma-separated, for a rotation.
2. **Wi-Fi.** Over USB, the bench's Wi-Fi step (or by hand)
   `api wifi "{\"ssid\":\"...\",\"password\":\"...\"}"`. It is kept in the
   default NVS, namespace `config`, blobs `ssid` and `password` (the
   hi-fi's places, so a board moved between the two keeps its network).
   A board with no `config` blobs but a network in IDF's own Wi-Fi store
   joins that one. NVS is never erased at boot: if it won't open, the
   trial fails and an update rolls back instead of losing the network.
3. **The bench link.** The USB bringup's remote step sends
   `api remote {"url":"wss://...","user":...,"password":...,"key":...}`
   (the Access token and the board's own key, which the bench mints); they
   are kept in the `settings` partition and `GET remote` never returns
   them. From then on the board dials the bench at boot and the bench
   reaches it as `--dev <its Wi-Fi MAC>`.
   Only `wss://` links are dialled: a board whose link is an `mqtt(s)://`
   one (an older hi-fi setup) gets no dial-in, fails its trial and rolls
   back, so check `GET remote` on it before sending this app over the air.

## The self-test

`api selftest "{}"` (console) or `POST selftest` (remote) answers one line:

    API {"pass":true,"checks":[{"name":"heap","pass":true,"value":"..."},...]}

`pass` is true when every check passed. The checks:

| name | passes when | value |
|---|---|---|
| `heap` | internal RAM free is over 12000 B | free now, at boot, and how far the boot figure is over the floor |
| `network` | Wi-Fi is joined, and the bench link is up if one is set | `Wi-Fi not joined`, `no Wi-Fi set`, `Wi-Fi up, relay not reached`, `Wi-Fi and relay up`, `Wi-Fi up, no relay set` |
| `trial` | the running image's state is known | `<slot> <state>`, e.g. `ota_0 valid` |
| `selftest_fail` | never (only in an `RR_SELFTEST_FAIL` build) | `built with RR_SELFTEST_FAIL` |

Add your app's own with `rr_selftest_add(t, "name", pass, "value")` in
`op_selftest()`. A board not yet on Wi-Fi fails `network`; the profile
lists that as expected offline, so its first USB self-test still reads as
a pass on the bench.

## The trial

A new image (an update over the link, or a USB flash into an OTA slot)
boots pending. `rr_relay.c` marks it good once every trial input holds:
Wi-Fi joined (when set), the bench link up (when set), heap over the
floor, and your own checks in `trial_inputs()`. If one still fails after
120 s, or one fails for good, it marks the image bad and restarts into the
one before; the bench sees `ota_valid` or `rolled_back`. To prove the
rollback on a board, build a copy with `CONFIG_RR_SELFTEST_FAIL=y`
(menuconfig, "Remote-ready"): its self-test fails `selftest_fail` and its
trial fails, so the board goes back by itself. Never ship that build.

## The bench profile

`bench.json`, which `fw publish` hands to the bench:

    "api": {"style": "json", "prefix": "api", "reply": "API", "info": "info", "wifi": "wifi"},
    "selftest": {"cmd": "api selftest \"{}\"", "pass": "API {\"pass\":true",
                 "fail": "API {\"pass\":false", "timeout": 30,
                 "expected": [{"checks": ["network"], "why": "not on the network yet", "offline": true}]},
    "remote": {"style": "hifi-ws", "ota": "@NAME@.bin"}

Set `board` and `usb` to your board's name and USB bridge.

## Not in it

No HTTP server on the board and no open port: the board only dials out.
No eFuse writes, no secure boot, no flash encryption: a product image
should add them. Only the app slot changes over Wi-Fi; a new bootloader,
partition table or recovery image needs USB.
