# Firmware with fw in the Hive

fw builds, tests and boots ESP32 firmware written with ESP-IDF, from an
agent's sandbox. It's a few small commands, not a pipeline. Use the ones your
work needs, in any order. `bin/fw help` lists them all. Each script in
`scripts/` also runs on its own with `python3 scripts/<name>.py --help`.

## Before you start

- `fw doctor [PROJECT]` shows what the project pins (`fw.json`), which IDF
  versions are installed and where, whether QEMU is there, and which serial
  ports the sandbox can see.
- `fw setup --project PROJECT` installs the pinned IDF release: a shallow
  clone with submodules, the xtensa toolchains, cmake, ninja, the IDF python
  env (built on a uv python: 3.10 for IDF 4.x, 3.12 for 5.x and later), and
  Espressif's QEMU with the one library it needs that the host lacks
  (libslirp, from Ubuntu's .deb, checked by sha256). It skips anything a
  root already has.
  - The roots are searched in this order: `$FW_TOOLS`, `/hive/tools/fw` (the
    Hive's shared folder, read-only to you), then `$FW_HOME` (default
    `~/.cache/fw`).
  - Without a shared copy, setup installs into your HOME. ESP-IDF v4.4.8
    took about 15 minutes and 2 GB there on a loaded machine.
  - A version every agent needs belongs in `/hive/tools/fw`. Ask a warden
    (T1) to run `python3 scripts/setup.py --idf vX.Y.Z --prefix /hive/tools/fw`
    from this module.
- Pin the project in its `fw.json` (every key is optional):

  ```json
  {"idf": "v4.4.8", "target": "esp32", "host_tests": "test/host",
   "qemu": {"expect": "my_app: ready", "timeout": 60, "settle": 3, "psram": "4M", "flash": "8MB"}}
  ```

  Firmware that is an overlay on an upstream tree, like the hifi receiver's
  (squeezelite-esp32 plus a patch and a component), adds these keys:
  - `"prepare": "sh tools/fetch.sh {src}"` runs before each build, in the
    project dir, and makes the IDF project at `"src"` (default
    `{project}/.fw-src`; `{home}` works too).
  - `"python": ["setuptools<70", "protobuf>=3.20,<4"]` lists packages the
    build needs beside the IDF's own. They're installed once into
    `~/.cache/fw/pyextra/` and put first on `PYTHONPATH`, so they work even
    when the IDF venv is the shared, read-only one.
  - `"qemu": {"sdkconfig": ["CONFIG_ESP32_REV_MIN_0=y", ...]}` holds config
    lines for QEMU only, because QEMU emulates a revision-0 chip.
    `fw build --qemu` lays them over the project's sdkconfig in a second
    build dir, `build-qemu/`, so `build/` stays the board's. `fw qemu` boots
    `build-qemu/` when these lines exist.
    - An overlay line replaces the option's deprecated alias too (IDF's
      `sdkconfig.rename` files), so `CONFIG_FLASHMODE_QIO=y` left in an old
      sdkconfig can't put the board's value back.
    - QIO flash fails in QEMU: IDF can't set the emulated flash's QE bit,
      and `esp_flash_init_default_chip()` fails an assert before
      `app_main`. Firmware built for QIO needs
      `"# CONFIG_ESPTOOLPY_FLASHMODE_QIO is not set"` and
      `"CONFIG_ESPTOOLPY_FLASHMODE_DIO=y"` here.
    - With `CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_SILENT`, the asserts in
      one function can share one `__assert_func` call, so the PC in
      `assert failed: 0x…` can name the wrong line. Read the log lines
      before it for the step that failed, and look for the branches into
      that call (`objdump -d`). The ELF to read is the image that boots,
      which for squeezelite is `recovery.elf`.

  With no `fw.json`, fw reads the IDF version from `.idf-version` or from a
  Dockerfile's `IDF_CLONE_BRANCH_OR_TAG`, which is how squeezelite-esp32
  pins v4.4.8. Otherwise it uses `v5.5.6`.

## The commands

| command | what it does |
|---|---|
| `fw build [P] [--qemu] [--no-prepare]` | Runs `prepare`, then `idf.py set-target <target>` on a fresh tree, then `idf.py build`. The IDF's environment is computed from `idf_tools.py export`, without sourcing `export.sh`. |
| `fw idf [P] -- ARGS` | Any `idf.py` command with the right environment: `size-components`, `fullclean`, `-D SDKCONFIG_DEFAULTS=... reconfigure`. |
| `eval "$(fw env [P])"` | Puts `IDF_PATH`, `PATH` and `FW_QEMU` in your shell, for scripts of your own. |
| `fw host-test [P]` | The project's unit tests on the host. Runs the `host_tests` dir's executable `run`, its `CMakeLists.txt` (cmake and ctest), its `Makefile` (`make test` if it has that target, else its default; `FW_UNITY=...` passed), or else each `test_*.c` as one program, built with gcc, ASan and UBSan against the vendored Unity. A test file names its sources in header lines: `// fw-src: main/volume.c`, `// fw-inc: main`, `// fw-cflags: -DX`. |
| `fw qemu [P] [--http 'GET /path' ...] [--hold S]` | Lays `build/flasher_args.json`'s binaries into one flash image (`build/fw-flash.bin`) and boots it in QEMU's `esp32` or `esp32s3` machine with `-no-reboot` (so a reset ends the run). It watches the UART: it passes once the expect pattern shows and nothing fatal follows for `settle` seconds. It fails on a panic, `abort()`, an assert, a watchdog, a brownout, `ESP_ERROR_CHECK`, a reboot, or no app in flash. With HTTP requests (below) it also makes them over QEMU's Ethernet and judges the replies. The log goes to `build/fw-qemu.log`. |
| `fw flash [P] --port PORT` | `idf.py -p PORT flash`. `PORT` is `/dev/ttyUSB0` for a board on this machine, or `rfc2217://host:4000` for a board elsewhere. `FW_PORT` sets a default. |
| `fw monitor [P] --port PORT [--seconds 15] [--expect RE]` | Resets the chip through DTR/RTS, reads the UART for a while with no tty, and gives the same verdict as `fw qemu`. The log goes to `build/fw-monitor.log`. |
| `fw ports` | The serial ports this sandbox can see. |
| `fw bootlog LOG [--expect RE]` | Judges a log you already have. |

Builds run at nice 10 (`FW_NICE`), with ninja capped at 4 jobs
(`FW_JOBS`), because the machine is shared. Raise `FW_JOBS` when it's
idle.

### What QEMU can and can't tell you

QEMU emulates the ESP32 core, flash, PSRAM (`psram`), timers, the UART and an
open_eth Ethernet. It has no Wi-Fi, Bluetooth, I2S codec or I2C devices. A
boot that reaches `app_main` and stays up catches partition-table, sdkconfig,
PSRAM, early-init and crash-on-boot faults, which is most of what breaks a
build. Code that waits on Wi-Fi or a codec should log a line of its own once
it's past init, and `expect` should match that line. Past that, the network
is the way in (next), and the rest needs a board or host tests.

### Talking to the firmware over QEMU's Ethernet

`fw qemu` gives the guest QEMU's open_eth on user networking: the firmware
gets 10.0.2.15 by DHCP, and fw forwards a free port on 127.0.0.1 to its
port 80 (`"http_port"` for another). It needs two things:

- **Requests to make.** `"qemu": {"http": [...]}` in `fw.json`. Once the
  expect line shows, fw waits for the first request to get an answer (DHCP
  and the server take a few seconds), then makes each one in order. The
  run fails on a wrong status or a reply that doesn't match `expect`:

  ```json
  "http": [{"get": "/api/v1/info", "expect": "\"model\":\"hifi-lite\""},
           {"post": "/api/v1/volume", "json": {"volume": 40}, "expect": "\"volume\":40,"},
           {"post": "/api/v1/volume", "json": {"volume": 400}, "status": 400}]
  ```

  `status` defaults to 200. `"body": "text"` sends text instead of
  `json`. The requests and replies are added to the end of the log. By
  hand, `--http 'GET /api/v1/state'` or `--http 'POST /api/v1/volume
  {"mute": true}'` (repeat it) replaces the list and prints the replies.
  `--hold 300` keeps a passing run up five more minutes and prints the URL,
  for curl of your own; run it in the background.
- **Ethernet in the firmware.** QEMU emulates an OpenCores MAC with a
  DP83848-like PHY, not the ESP32's EMAC. Firmware with its own open_eth
  code (IDF's `example_connect` with `CONFIG_EXAMPLE_USE_OPENETH`) just
  needs that in `qemu.sdkconfig`. Firmware that brings up Ethernet through
  the EMAC (any RMII PHY: LAN8720, IP101, RTL8201, KSZ80xx) needs no change.
  `"eth": "emac"` sets `CONFIG_ETH_USE_OPENETH` and links a shim (below)
  that hands those constructors IDF's open_eth MAC and DP83848 driver. The
  firmware still has to be told it has Ethernet, in its own way. For
  squeezelite that's an NVS setting (below); the pins mean nothing to QEMU.

### What QEMU lacks, and the shims for it

Some calls never return in QEMU, or crash it, because the hardware isn't
emulated. For each one, `fw build --qemu` can link a small file from
[qemu/](../qemu/) into `build-qemu/` with `-Wl,--wrap`. The firmware's own
code is unchanged, and the board's `build/` never sees the shim.

| `fw.json` `"qemu"` key | what it wraps | why |
|---|---|---|
| `"eth": "emac"` | `esp_eth_mac_new_esp32`, `esp_eth_phy_new_{lan87xx,ip101,rtl8201,ksz80xx,...}` | QEMU's Ethernet is open_eth, not the EMAC ([openeth.c](../qemu/openeth.c)) |
| `"adc1": 0` or `{"6": 1700}` | `adc1_get_raw`, which returns the raw value given for the channel (else 0) | no SAR ADC: the read spins forever holding a spinlock, and the core's tick stops, so the boot hangs silently ([adc.c](../qemu/adc.c)) |
| `"wifi": "off"` | `esp_wifi_start`, `_stop`, `_connect`, `_disconnect`, `_scan_start`, `_scan_stop`, which all succeed | no radio: PHY calibration panics with `LoadStorePIFAddrError` in `register_chipv7_phy` ([nowifi.c](../qemu/nowifi.c)) |

Two more keys shape the flash image `fw qemu` boots, with no rebuild:

- `"nvs": {"settings": {"config": {"eth_config": "model=LAN8720,mdc=23,mdio=18"}}}`
  is *partition → namespace → key → value* (a string, or a whole number
  as an i32). fw writes the partition with the IDF's own
  `nvs_partition_gen.py` on every run, so each boot starts from these
  values, as on a fresh board.
- `"boot": "ota_0"` writes otadata that selects that OTA slot, so the
  bootloader starts it instead of the factory app. squeezelite's factory
  app is its recovery, which skips Ethernet.

A boot that seems to stop with no fatal line, its log timestamps frozen, is
usually a busy-wait on missing hardware. Run QEMU with `-gdb tcp::1234` and
`xtensa-esp32-elf-gdb -ex 'target remote :1234' -ex 'thread apply all bt'`
to see where. That's how the ADC wait was found.

## Boards

A board plugged into this machine isn't visible inside a sandbox. `fw ports`
says so. There are two ways to reach one:

- **Over the network.** Run `esp_rfc2217_server.py -p 4000 /dev/ttyUSB0`
  (it ships in esptool) on the machine with the board. The person's laptop
  or a Pi on the bench both work. Then flash and monitor with
  `--port rfc2217://<host>:4000`. Reaching a host on the LAN needs a warden
  to grant the egress (`hive egress grant <host>:4000`, T2).
- **On this machine.** A warden passes the `/dev/ttyUSB*` device into your
  sandbox (T2).

Flashing changes a physical board, so flash only a board you've been asked to
work on.

## Test and score

- **Test**, `bin/fw test`, run on every change. It runs the unit tests in
  `tests/` (standard-library `unittest`, no toolchain needed, about 10 s):
  the boot-log verdicts, the flash image merge, the project pins and root
  lookup, the host-test runner against `examples/hello`, the setup helpers
  and the CLI. It also checks that every example has a proof made on its
  current files (below).
- **Proofs**, `bin/fw check-all [NAME] [--from DIR]`. This is not the
  per-change test. It covers every firmware fw serves:
  - `examples/<name>/`, this module's own examples;
  - the real products in [targets.json](../targets.json), read from
    `$HIVE_PROJECTS`. The first is `hifi-lite`, the receiver's Lite
    firmware in hifi-receiver-2's `firmware/`.

  `--from DIR` checks one target from another place, such as a branch that
  hasn't merged. Each firmware is copied to a work dir and run through
  `fw build`, `fw host-test` and `fw qemu` (with `fw build --qemu` first when
  it has QEMU-only config). A target's pins in targets.json fill in what its
  own `fw.json` lacks, and its IDF tree is kept in
  `~/.cache/fw/check/<name>/` so later runs rebuild incrementally.

  check-all writes `proof/<name>.json`: each stage's verdict, the IDF
  version and a hash of the firmware's files. Run it after you change what a
  firmware exercises, then commit `proof/`.
- **Score**, `bin/fw score`, higher is better. It is the share of stages
  (build, host-test, qemu, for every example and target) that a current
  proof shows passing, as a percentage. A stale proof counts nothing. A
  target whose files this machine can't see is judged on its proof alone.
  It reads files only. To raise it:
  - make a failing stage of the real product pass;
  - add the next real firmware to targets.json;
  - add an example that proves something new: another IDF version, another
    chip, or a feature real firmware needs.

## Be critical of the tools

When fw gets in your way, fix it here if the fix is small: a missing flag, a
false verdict in `bootlog.FATAL`, a step setup misses. Add a test for it. A
bigger fix gets its own brief (`hive agent brief --project hive-fw ...`).
Don't work around fw in your own project without saying so in your account.
What the first real user needed is in [first-user.md](first-user.md).
