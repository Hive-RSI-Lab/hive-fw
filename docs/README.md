# Firmware with fw in the Hive

fw builds, tests and boots ESP32 firmware written with ESP-IDF, from an
agent's sandbox. It's a few small commands, not a pipeline. Use the ones your
work needs, in any order. `bin/fw help` lists them all. Each script in
`scripts/` also runs on its own with `python3 scripts/<name>.py --help`.

## Before you start

- `fw doctor [PROJECT]` shows what the project pins (`fw.json`), which IDF
  versions are installed and where, whether QEMU is there, and which serial
  ports the sandbox can see.
- `fw setup [PROJECT]` installs the pinned IDF release (run from a repo whose
  firmware sits in a subfolder, it takes each `fw.json` it finds there): a
  shallow clone with submodules, any the Hive's proxy refuses over git
  (github.com/nghttp2/nghttp2, which IDF needs) unpacked from GitHub's
  codeload tarball of the pinned commit and marked `update=none`, the xtensa toolchains, cmake, ninja, the IDF python
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
  - `"env": {"HIFI_BOARD": "core"}` sets variables for every step: the
    prepare step, `idf.py` (so `fw build`, `fw idf`, `fw env`) and the host
    tests. A tree that builds several boards picks one this way, as the
    receiver's `fetch.sh` reads `HIFI_BOARD`.
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
| `fw pins [P] [--board F]` | Just the pin map against the board's KiCad netlist (`.net`, `.kicad_sch`, `.kicad_pcb`): the part of `fw constraints` hwde's side runs alone. |
| `fw dts [P] [--dts F] [--board F] [--ref U]` | A Linux board's device tree pin mux against the KiCad netlist: each i.MX `fsl,pins` pad (and STM32 / sunxi / Rockchip pinctrl pads) is on the SoC, connected, and carries the net its function names (PAD-NONE, PAD-NC, PAD-NET). Config: fw.json `"dts"` (scripts/dts.py). |

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

## What the firmware needs from the board

`fw constraints` is fw's one checker. It puts everything the firmware takes
for granted about the hardware into one report, and every rule the firmware
or the board breaks in it as a coded problem, so the board is designed against it instead of
discovering it at bring-up. Each line says where in the firmware it comes
from. It reads files only, needs no IDF and takes about a second, so the
board side can run it on every change:

    $HIVE_MODULES/hive-fw/bin/fw constraints --target hifi-plus          # the text report
    $HIVE_MODULES/hive-fw/bin/fw constraints --target hifi-plus --json   # the same as data
    $HIVE_MODULES/hive-fw/bin/fw constraints FIRMWARE_DIR --board my.net --out fw-constraints.json

What it reads, and the needs it turns that into:

- **The sdkconfig**: the chip and its lowest revision; flash size; PSRAM
  (and whether the firmware aborts without it); the crystal; Wi-Fi and
  Bluetooth, which mean an antenna keep-out and a 3.3 V supply that gives
  500 mA or more for transmit peaks; the brown-out threshold the rail has
  to stay above.
- **The partition table**: where it ends, against the flash size;
  partitions that overlap each other or the table (PART-OVERLAP), aren't
  aligned (PART-ALIGN, apps on 64 KB), share a name or leave no app
  (PART-BAD), or OTA slots without otadata (PART-OTA). `images` (a JSON of
  partition -> bytes a build put there) checks each image against its own
  slot (PART-IMAGE). With a build (`--build`, or the project's own `build/`), it also checks the app
  image against its partitions (the OTA slots, or else the factory one) and
  gives DRAM, IRAM, flash and PSRAM use from the ELF.
- **The sources**: the GPIOs the firmware drives (read by `fw pins`'s
  reader), each against the ESP32's rules: the flash pins (6-11) and a
  WROVER's PSRAM pins (16/17) are taken; 34-39 are input only, with no
  pull-ups inside; strapping levels at reset (GPIO0 high, GPIO2 low or
  floating, GPIO12 low for 3.3 V flash);
  ADC2 reads nothing while Wi-Fi is on (ADC2-WIFI); one GPIO given two
  unrelated jobs is PIN-SHARED. PSRAM off while other settings put data in
  it (Bluetooth's buffers, malloc) is SPIRAM-OFF. An RMII PHY (`model=LAN8720`, or `#define ...RMII...`) puts the
  internal EMAC's fixed pins (19, 21, 22, 25, 26, 27, and the clock on
  0/16/17) on the list. The report also lists the I2C addresses the firmware
  talks to (`#define X_ADDR 0x48`: the board's address straps have to match)
  and the I2C bus speed, which sizes the pull-ups. Every report adds UART0,
  EN and GPIO0 for flashing.
- **The board**, when one is named (`--board`, or the config's `board`): the
  ESP32 part's name against the module needs (ESP32-WROVER-E-N8R8 means
  8 MB of flash, 8 MB of PSRAM, a v3 chip), and the pin map against its nets
  (`fw pins`'s check, PIN-NET), and the board's pull resistors on the
  straps (from a `.net`): one that holds GPIO0 low, or GPIO2 or GPIO12 high,
  at reset is STRAP-0/2/12. A strap the firmware uses with no such resistor
  stays a need, not a problem: the hifi core board's IO12 drives a PCM5102A
  DIN, high-Z at boot, and is fine.

Each finding is either a **need** (something the board must provide) or a
**problem** (something the firmware or the board breaks), printed as
`PROBLEM CODE: what` (in the JSON, `code`; the codes: PIN-NET, PIN-SHARED,
PIN-FLASH, PIN-INPUT, PIN-RMII, STRAP-0/2/12, ADC2-WIFI, SPIRAM-OFF, PART-*,
BOARD-*). The command exits
0 with no problems, 1 with one, and 2 when there was nothing to read. Where
to look is set by `constraints` in fw.json or in a target's pins;
`pinmap`'s keys fill in what it leaves out:

    "constraints": {"sdkconfig": "board/core/sdkconfig.defaults",
                    "partitions": "board/core/partitions.csv",
                    "sources": ["components/hifi/esp/hifi_board_core.h", "patches/*.patch"],
                    "when": "HIFI_PLUS",            (Kconfig `default ... if X` lines for this board only)
                    "board": "../boards/core/kicad/core.net",
                    "ignore": ["EXP_LED"],          (names that aren't GPIOs: an expander's bit)
                    "images": "sizes.json"}         (optional: image bytes per partition)

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
- **Check-all**, `bin/fw check-all [NAME] [--from DIR]`. This is not the
  per-change test. It builds, host-tests and boots (`fw build`,
  `fw host-test`, `fw qemu`, with `fw build --qemu` first when there's
  QEMU-only config) every firmware fw knows:
  - `examples/<name>/`, this module's own examples. Their run is recorded
    in `proof/<name>.json` (each stage's verdict, the IDF version, a hash of
    the files), which the test holds to their files: run it after you change
    an example, then commit `proof/`;
  - the real products in [targets.json](../targets.json), read from their
    project's main branch in the clone under `$HIVE_PROJECTS` (`--from DIR`:
    a branch not merged). `hifi-lite`, `hifi-plus` and `hifi-ultra` are the
    receiver's firmware for its three boards. Their runs are printed, not
    recorded here: the product's own check proves its firmware. A target's
    pins fill in what its own `fw.json` lacks, and its IDF tree is kept in
    `~/.cache/fw/check/<name>/` so later runs rebuild incrementally.
- **Score**, `bin/fw score`, higher is better: planted firmware faults fw's
  checks catch, minus false alarms. The corpus is [faults/](../faults/): a
  copy of a real firmware and its board (`faults/lite/`: the receiver's Lite
  firmware and lite-main's netlist) with a `fw.json` for `fw constraints`, and `faults.json`,
  the faults planted on it (find/replace edits: a wrong GPIO, a strap pin,
  ADC2 with Wi-Fi, a partition past the flash, an image over its slot...).
  Each fault is planted in a scratch copy of the base (in /dev/shm), and it
  is caught when `fw constraints` reports a problem with it planted that it
  doesn't on the clean base; every problem on a clean base costs a point.
  Each fault names its kind (`expect`) and `site`; a second fault of the
  same kind at the same site is a near-duplicate and counts nothing. It
  reads only this module's files, in about a second. It has no top:
  to raise it,
  - make a check catch a fault it misses (`fw score` prints each);
  - add a fault kind real firmware gets wrong, with the check that catches
    it (a fault its check can't catch yet is fine too: it shows what to
    build next);
  - add a base: another board's firmware (the core board's Plus and Ultra),
    with its own clean files, on which `fw constraints` must find no problem.
  The test holds the corpus: every fault plants, no base raises an error,
  and a caught fault is caught by the check it names in `expect`.

## Be critical of the tools

When fw gets in your way, fix it here if the fix is small: a missing flag, a
false verdict in `bootlog.FATAL`, a step setup misses. Add a test for it. A
bigger fix gets its own brief (`hive agent brief --project hive-fw ...`).
Don't work around fw in your own project without saying so in your account.
What the first real user needed is in [first-user.md](first-user.md).

## STM32 (arm-none-eabi)

For a bare-metal STM32 project (no IDF): `fw stm32 setup` installs the pinned
xPack arm-none-eabi-gcc (sha256 checked) under the same roots as the IDF;
`fw stm32 doctor` shows what is found; `fw stm32 build` runs `make` with
`ARM_PREFIX=<bin>/arm-none-eabi-` (the Makefile should use `$(ARM_PREFIX)gcc`);
`fw host-test` runs the project's host unit tests as for any project;
`fw stm32 pins` checks the pin header against the chip's pin table
(`data/stm32g474_lqfp64.json`, DS12288 Rev 6, parsed from the PDF by word
position, since pdftotext loses the table) and the board's pinmap.json. Its
fw.json section is described in `scripts/stm32.py`. Flash with `dfu-util` on
the ROM DFU (0483:df11); a blank chip needs BOOT0 high (AN2606). st.com
refuses sandbox downloads; web.archive.org copies of its PDFs work.
