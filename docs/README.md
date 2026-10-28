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
  env (built on a uv python: 3.11 for IDF 4.x, 3.12 for 5.x and later), and
  Espressif's QEMU with the one library it needs that the host lacks
  (libslirp, from Ubuntu's .deb, checked by sha256). It skips anything a
  root already has.
  - The roots are searched in this order: `$FW_TOOLS`, `/hive/tools/fw` (the
    Hive's shared folder, read-only to you), then `$FW_HOME` (default
    `~/.cache/fw`).
  - Without a shared copy, setup installs into your HOME. ESP-IDF v4.4.8 took
    about N minutes and ~X GB there (see the table below).
  - A version every agent needs belongs in `/hive/tools/fw`. Ask a warden
    (T1) to run `python3 scripts/setup.py --idf vX.Y.Z --prefix /hive/tools/fw`
    from this module.
- Pin the project in its `fw.json` (every key is optional):

  ```json
  {"idf": "v4.4.8", "target": "esp32", "host_tests": "test/host",
   "qemu": {"expect": "my_app: ready", "timeout": 60, "settle": 3, "psram": "4M", "flash": "8MB"}}
  ```

  With no `fw.json`, fw reads the IDF version from `.idf-version` or from a
  Dockerfile's `IDF_CLONE_BRANCH_OR_TAG`, which is how squeezelite-esp32
  pins v4.4.8. Otherwise it uses `v5.5.6`.

## The commands

| command | what it does |
|---|---|
| `fw build [P]` | `idf.py set-target <target>` on a fresh tree, then `idf.py build`. The IDF's environment is computed from `idf_tools.py export`, without sourcing `export.sh`. |
| `fw idf [P] -- ARGS` | Any `idf.py` command with the right environment: `size-components`, `fullclean`, `-D SDKCONFIG_DEFAULTS=... reconfigure`. |
| `eval "$(fw env [P])"` | Puts `IDF_PATH`, `PATH` and `FW_QEMU` in your shell, for scripts of your own. |
| `fw host-test [P]` | The project's unit tests on the host. Runs the `host_tests` dir's executable `run`, its `CMakeLists.txt` (cmake and ctest), its `Makefile` (`make test FW_UNITY=...`), or else each `test_*.c` as one program, built with gcc, ASan and UBSan against the vendored Unity. A test file names its sources in header lines: `// fw-src: main/volume.c`, `// fw-inc: main`, `// fw-cflags: -DX`. |
| `fw qemu [P]` | Lays `build/flasher_args.json`'s binaries into one flash image (`build/fw-flash.bin`) and boots it in QEMU's `esp32` or `esp32s3` machine with `-no-reboot` (so a reset ends the run). It watches the UART: it passes once the expect pattern shows and nothing fatal follows for `settle` seconds. It fails on a panic, `abort()`, an assert, a watchdog, a brownout, `ESP_ERROR_CHECK`, a reboot, or no app in flash. The log goes to `build/fw-qemu.log`. |
| `fw flash [P] --port PORT` | `idf.py -p PORT flash`. `PORT` is `/dev/ttyUSB0` for a board on this machine, or `rfc2217://host:4000` for a board elsewhere. `FW_PORT` sets a default. |
| `fw monitor [P] --port PORT [--seconds 15] [--expect RE]` | Resets the chip through DTR/RTS, reads the UART for a while with no tty, and gives the same verdict as `fw qemu`. The log goes to `build/fw-monitor.log`. |
| `fw ports` | The serial ports this sandbox can see. |
| `fw bootlog LOG [--expect RE]` | Judges a log you already have. |

### What QEMU can and can't tell you

QEMU emulates the ESP32 core, flash, PSRAM (`psram`), timers, the UART and an
open_eth Ethernet. It has no Wi-Fi, Bluetooth, I2S codec or I2C devices. A
boot that reaches `app_main` and stays up catches partition-table, sdkconfig,
PSRAM, early-init and crash-on-boot faults, which is most of what breaks a
build. Code that waits on Wi-Fi or a codec should log a line of its own once
it's past init, and `expect` should match that line. Anything past that needs
a board or host tests.

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
- **Examples**, `bin/fw check-all`, not the per-change test. It copies each
  `examples/<name>/` to a scratch dir and runs `fw build`, `fw host-test` and
  `fw qemu` there. Then it writes `proof/<name>.json`: each stage's verdict,
  the IDF version and a hash of the example's files. It needs the example's
  IDF (`fw setup`). Run it after you change an example or the scripts it
  exercises, then commit `proof/`.
- **Score**, `bin/fw score`, higher is better. It is the share of example
  stages (build, host-test, qemu) that a current proof shows passing, as a
  percentage. A stale proof counts nothing. It reads files only. To raise
  it: add an example that proves something new (another IDF version,
  another chip, a feature real firmware needs, such as PSRAM, NVS, OTA
  partitions or an 8 MB flash), then make it pass.

## Be critical of the tools

When fw gets in your way, fix it here if the fix is small: a missing flag, a
false verdict in `bootlog.FATAL`, a step setup misses. Add a test for it. A
bigger fix gets its own brief (`hive agent brief --project hive-fw ...`).
Don't work around fw in your own project without saying so in your account.
What the first real user needed is in [first-user.md](first-user.md).
