# hive-fw

The fw module lets Hive agents build, test and boot ESP32 firmware written
with ESP-IDF, so a hardware project gets its firmware alongside its boards.
It is a handful of small commands that agents combine as they need, not a
pipeline.

- `bin/fw` runs the commands: `setup`, `doctor`, `build`, `idf`,
  `host-test`, `qemu`, `flash`, `monitor`, `ports`, `bootlog`, `constraints`, `pins`,
  `test`, `check-all` and `score`.
- `scripts/` holds one standard-library python script per job, each tested
  in `tests/`.
- `host/unity/` is Unity v2.6.0, for host unit tests ([NOTICE](NOTICE)).
- `faults/` is the score's corpus: real firmware with planted faults.
- `examples/hello/` is the module's own proof: an IDF app with a host test
  and a QEMU boot.
- [docs/README.md](docs/README.md) is how an agent uses fw in the Hive.
- [docs/first-user.md](docs/first-user.md) is what the hifi receiver's
  firmware needed from it.

## Test and score

- **Test**: `bin/fw test`, the unit tests in `tests/`. It needs no
  toolchain and fails when an example's proof is missing or stale.
- **Score**: `bin/fw score`, higher is better: the planted faults in
  `faults/` that `fw constraints` (fw's one checker) catches, minus false alarms on the clean
  firmware. It reads only this module's files.
