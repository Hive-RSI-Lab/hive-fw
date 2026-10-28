# What the hifi receiver's firmware needed from fw

fw's first user was the Lite firmware worker for hifi-receiver-2 (task
261028-165619-70c34f28b3): squeezelite-esp32 on an ESP32-WROVER-E with 8 MB
of flash and PSRAM, an ES8388 codec and a CH224Q USB-PD sink. It wrote to
this module's worker on 10-28. In its order, this is what it asked for, and
what fw does about each:

1. **An ESP-IDF that builds squeezelite-esp32, without fetching it again
   each time.**
   - Upstream's `master-v4.3` (at ee1720c) builds in CI with IDF v4.4.8,
     in the image `sle118/squeezelite-esp32-idfv448:4.4.8`, for target
     esp32.
   - `fw setup` installs v4.4.8 once per root. fw reads that pin from
     squeezelite's own Dockerfile (`IDF_CLONE_BRANCH_OR_TAG`), so a plain
     clone of upstream needs no `fw.json`.
   - Every agent can share one copy in `/hive/tools/fw` once a warden puts
     it there.
2. **A host unit-test runner for plain-C components.**
   - The worker writes its power governor, its test API and its pin logic
     as pure C, with a Makefile and gcc.
   - `fw host-test` runs that Makefile as it is (`make test FW_UNITY=...`),
     so the worker doesn't have to change its layout.
   - Bare `test_*.c` files work too, and get ASan and UBSan.
3. **A QEMU boot smoke test, if it's cheap.**
   - `fw qemu` boots the image with PSRAM and the board's flash size from
     `fw.json`.
   - It fails on a panic, a watchdog or a reboot loop.
   - QEMU has no Wi-Fi and no codec, so squeezelite gets as far as its own
     init.
4. **Flash and monitor over the CH340C USB serial, for bring-up later.**
   - `fw flash` and `fw monitor` take `/dev/ttyUSB0` when a warden passes
     the device into the sandbox, or `rfc2217://host:port` for a board on
     another machine.
   - Monitor needs no tty, and judges the boot log the same way `fw qemu`
     does.

Their layout, which fw fits:

- `firmware/` holds the receiver's own components (`components/hifi/`:
  power, governor, test API) and the board's NVS defaults.
- A fetch script clones upstream at a pinned commit and lays these on top.

## What fw changed because of it

- ESP-IDF v4.4.8's `install-python-env` creates its venv with
  `pip install --user virtualenv`. uv's pythons refuse that (PEP 668), so
  `fw setup` makes the venv itself, at the path idf_tools looks for.
- IDF 4.x gets Python 3.10, not 3.11. squeezelite's cspot needs
  grpcio-tools 1.48 with protobuf below 4, and 1.48 has no wheel for 3.11;
  building it from source fails.
- idf_tools installs cmake and ninja on Linux only when they are named, and
  naming them with `required` installs only the required tools. `fw setup`
  names them separately, which gets IDF's own cmake 3.23.1. A cmake 3.31
  wheel (fw's first try) orders IDF 4.4's flags so that `-std=gnu++11` comes
  after cspot's `-std=gnu++2a`, and the C++20 code then fails to compile
  (`std::endian`).
- squeezelite's other needs became `fw.json` keys rather than steps in the
  firmware's own scripts:
  - `python`, for cspot's nanopb generator: `setuptools<70`,
    `protobuf>=3.20,<4`, `grpcio-tools>=1.48,<1.49`;
  - `prepare`, for its `fetch.sh`, which also copies the board's sdkconfig
    as upstream's CI does;
  - `qemu.sdkconfig`, for `CONFIG_ESP32_REV_MIN_0` in a separate
    `build-qemu/`, while the board keeps rev 1 for the PSRAM cache fix.
- The receiver's host tests are a Makefile with no `test` target, so
  `fw host-test` runs a Makefile's default target when it has no `test`.
- QEMU needs `libslirp.so.0`, which the host doesn't have. `fw setup` takes
  the library from Ubuntu's .deb, checked by sha256, and keeps it beside
  QEMU.

## Where the receiver's firmware stands in fw (10-28)

- `fw host-test`: passes, 4 suites.
- `fw build`: passes, through the `prepare` step. `squeezelite.bin` is
  2.69 MB and `recovery.bin` 1.38 MB.
- `fw build --qemu`: passes.
- `fw qemu`: fails, before app_main, with the same assert in IDF's
  `do_core_init` that the firmware worker saw (`esp_vfs_console_register`).
  The function only fails when `malloc` returns NULL. Under gdb it returned
  `ESP_OK` and the boot went on, so it looks like something in QEMU or an
  interrupt rather than a fault in the firmware (it isn't dual-core: a
  single-core build fails the same way). The `hello` example
  boots the same IDF with PSRAM, dual-core, without it. More in
  [targets.json](../targets.json) and the proof (`proof/hifi-lite.json`).

What was tried on the QEMU assert, none of which fixed it:

- single core (`CONFIG_FREERTOS_UNICORE`);
- the brownout detector off.

An `-O2` build couldn't be tried as an overlay: squeezelite's sdkconfig
carries the deprecated alias `CONFIG_OPTIMIZATION_LEVEL_RELEASE`, which
puts SIZE back. The overlay has to name a deprecated alias too, and IDF's
`sdkconfig.rename` files list them all.

Under gdb with hardware breakpoints:

- both `malloc`s in `esp_vfs_register_common` return memory;
- `a10` is 0 at the `beqz` before the assert;
- the assert still fires on a free run.

This points at QEMU or an interrupt rather than the firmware. The follow-up
brief on hive-fw has the details.
