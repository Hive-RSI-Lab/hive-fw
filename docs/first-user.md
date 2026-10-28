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
  `pip install --user virtualenv`. The uv-managed Python fw uses refuses that
  (PEP 668), so `fw setup` makes the venv itself, at the path idf_tools
  looks for.
- idf_tools doesn't install cmake or ninja on Linux. `fw setup` puts them in
  the IDF's venv as wheels.
- QEMU needs `libslirp.so.0`, which the host doesn't have. `fw setup` takes
  the library from Ubuntu's .deb, checked by sha256, and keeps it beside
  QEMU.
