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
| `fw qemu [P]` | Lays `build/flasher_args.json`'s binaries into one flash image (`build/fw-flash.bin`) and boots it in QEMU's `esp32` or `esp32s3` machine with `-no-reboot` (so a reset ends the run). It watches the UART: it passes once the expect pattern shows and nothing fatal follows for `settle` seconds. It fails on a panic, `abort()`, an assert, a watchdog, a brownout, `ESP_ERROR_CHECK`, a reboot, or no app in flash. The log goes to `build/fw-qemu.log`. |
| `fw flash [P] --port PORT` | `idf.py -p PORT flash`. `PORT` is `/dev/ttyUSB0` for a board on this machine, or `rfc2217://host:4000` for a board elsewhere. `FW_PORT` sets a default. |
| `fw monitor [P] --port PORT [--seconds 15] [--expect RE]` | Resets the chip through DTR/RTS, reads the UART for a while with no tty, and gives the same verdict as `fw qemu`. The log goes to `build/fw-monitor.log`. |
| `fw ports` | The serial ports this sandbox can see. |
| `fw bootlog LOG [--expect RE]` | Judges a log you already have. |
| `fw pins [P] [--target NAME] [--board F] [--json]` | Checks the firmware's pin map against the board's KiCad netlist ([below](#pins-firmware-against-the-board)). |

Builds run at nice 10 (`FW_NICE`), with ninja capped at 4 jobs
(`FW_JOBS`), because the machine is shared. Raise `FW_JOBS` when it's
idle.

### What QEMU can and can't tell you

QEMU emulates the ESP32 core, flash, PSRAM (`psram`), timers, the UART and an
open_eth Ethernet. It has no Wi-Fi, Bluetooth, I2S codec or I2C devices. A
boot that reaches `app_main` and stays up catches partition-table, sdkconfig,
PSRAM, early-init and crash-on-boot faults, which is most of what breaks a
build. Code that waits on Wi-Fi or a codec should log a line of its own once
it's past init, and `expect` should match that line. Anything past that needs
a board or host tests.

## Pins: firmware against the board

`fw pins` reads the GPIOs the firmware assigns and the nets on the ESP32's
pins in the board's KiCad files, and checks every firmware pin. A pin passes
when, of all the nets on the ESP32, the one whose name best matches the
firmware's name for it is the net on that GPIO. So `#define PIN_AMP_SDZ 21`
passes on a board whose GPIO21 net is `AMP_SDZ_IO`, and fails, naming where
`AMP_SDZ_IO` really is, when it sits on GPIO23. A pin also fails when its
GPIO isn't connected, when no net matches its name, or when it's the
module's flash (6-11) or a WROVER's PSRAM (16, 17). Nets on the ESP32 that
the firmware never uses are listed as notes, since a board signal nobody
reads is often a missing feature. The exit code is 0 on a pass, 1 on a
fail and 2 when it couldn't check.

It reads, from the sources:
- `#define NAME N` (or `GPIO_NUM_N`) where NAME says PIN, GPIO or a signal
  (SDA, SCL, TX, BCLK, ...);
- `#define NAME ADC1_CHANNEL_6`, which it maps to the channel's GPIO;
- squeezelite-esp32's config strings: `dac_config`'s `bck= ws= do= di= mck=
  sda= scl=`, `set_GPIO`'s `14=ir,22=green`, and the buttons' JSON
  (`"gpio":13 ... "ACTRLS_VOLUP"`).

A patch's added lines count; its context lines don't. Names are matched by
their words, with the usual aliases (bck/BCLK, ws/LRCK, do/DOUT, green/LED,
TOGGLE/PLAY). A name that matches no net gets one in `nets`.

The firmware's `fw.json`, or a target's pins in targets.json, says where to
look:

```json
"pinmap": {"board": ["../boards/x/kicad/x.kicad_sch", "../boards/x/kicad/x.kicad_pcb"],
           "sources": ["main/board.h", "patches/*.patch"],
           "nets": {"PIN_MYSTERY": "BTN_PLAY"}, "ignore": ["PIN_SPARE"], "ref": "U5"}
```

- A `.net` netlist is read directly.
- A `.kicad_sch` is exported with kicad-cli (`$KICAD_CLI`, `kicad-cli` on
  PATH, or the Hive's `/hive/tools/kicad-*`). On a loaded machine the export
  can take a minute, and the time limit is `FW_KICAD_TIMEOUT` (180 s).
  Without kicad-cli, or past that limit, it reads the `.net` beside the
  schematic and says that file may be older.
- A `.kicad_pcb` is read through its pads' nets, so the check covers the
  layout that gets fabbed. Pads with no pin names are mapped with the
  WROOM-32/WROVER pad table.

**From hwde's side.** A board change can break the firmware, so a hwde
agent working on a board that firmware runs on can check it in a second
from the product's checkout, without the IDF:

```sh
$HIVE_MODULES/hive-fw/bin/fw pins firmware --target hifi-lite
$HIVE_MODULES/hive-fw/bin/fw pins firmware --target hifi-lite --board boards/lite-main/kicad/lite-main.kicad_pcb
```

`--target` takes the pinmap from targets.json, with the board paths
relative to the firmware dir given, so it checks the checkout's own board.
`--json` gives the verdict as data.

**Scored.** Firmware whose pins name a board gets a fourth stage, `pins`.
`fw check-all` runs it on the firmware's own files, and the proof keeps a
hash of the board files. The stage stops counting when the board changes.
`fw check-all NAME --only pins` re-proves just that stage in a second. It
keeps the other stages only when the proof was made on the same firmware
files.

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
  (build, host-test, qemu, and pins where a board is named, for every
  example and target) that a current
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
