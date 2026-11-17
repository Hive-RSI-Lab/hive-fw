# i2c-device: one chip on I2C, with the bus behind an interface, a mock bus and recovery

From the hi-fi Lite lab (ESP32-WROVER-E, IDF v4.4.8): the Lite hangs its
ES8388 codec (0x10) and CH224 USB-PD sink (0x22) off I2C port 0 (SDA 33,
SCL 32), and every board after it will hang more chips there.

- `@NAME@.c` / `include/@NAME@.h`: plain C, host-tested. A device at a
  7-bit address (0x08-0x77) on an `@NAME@_bus_t`: `probe` (an address-only
  transaction, writes nothing), `read`/`read_u8` and `write`/`write_u8`
  (8-bit registers, auto-increment, 1..`@UNAME@_MAX` bytes; a failed read
  leaves the caller's buffer as it was), `update` (read, change the masked
  bits, write only if they changed) and `check`: a table of
  `{reg, mask, want, what}` from the datasheet (a chip ID, reserved bits),
  read and compared, with the first difference as a sentence. `check` only
  reads, so a HIL case can run it on a product board without changing it.
  Errors are `ok nack timeout stuck arg bus diff` (`@NAME@_err_name`); each
  device counts its ok, nack, timeout, recovered and stuck transactions
  (`@NAME@_counts_json`).
- Policy: a NACK is an answer (nobody there, or the chip refused), retried
  only `nack_retries` times, 5 ms apart (a chip busy with its own write
  cycle), and never recovered. A TIMEOUT gets one `@NAME@_recover` and one
  more try; on a bus with nothing to recover with (no lines, no reset: a
  bus another driver owns) it gets the one more try alone, and nothing
  counts as recovered.
  Recovery lets both lines go; if SCL stays low it is `stuck` (clocking
  can't help); otherwise it clocks SCL up to 9 times until the chip holding
  SDA lets go (a chip reset mid-read holds SDA for its next 0 bit; 9 clocks
  cover 8 bits and the ack), sends a STOP (and, if the chip takes SDA again
  as SCL falls for it, clocks on inside the same 9 and tries the STOP up to
  3 times), calls the bus's `reset` to give
  the lines back to the controller, and says `stuck` if a line is still low.
  It never checks the lines before a transaction: on a shared bus another
  user's transaction may be on them. Recovery drives the lines itself, so
  a bus with `install` true must have a single user (or callers serialise).
- `mock/@NAME@_mock.c`: the host test's bus (not in CMakeLists.txt, not built
  for the board). Up to 4 chips with 256 registers and an auto-incrementing
  pointer; faults for the next n transactions (NACK, timeout, bus error);
  SDA held for k clocks or for good, and taken again on the STOP's falling
  clock; SCL held; and what happened (clocks, STOPs, resets, time waited,
  data bytes written). Use it for the chip driver you build
  on top: give the mock chip the datasheet's reset values.
- `@NAME@_esp.c` / `include/@NAME@_esp.h`: the bus on an ESP32.
  `@NAME@_esp_bus(&bus, &cfg)` with the port, pins, speed (default
  100 kHz), pull-ups and `install`. IDF 4.4 to 5.1 use the legacy
  `driver/i2c.h`: recovery takes the pins as open-drain GPIO and hands them
  back with `i2c_set_pin` and a FIFO reset. IDF 5.2 on use
  `driver/i2c_master.h` (one cached device handle per address, up to 8):
  recovery is `i2c_master_bus_reset`, which clocks the bus free itself. Its
  failed transfers all say INVALID_STATE, so a probe of the address tells a
  NACK from a held bus; its TIMEOUT is the bus lock's wait (another user's
  transfer), a BUS error with no recovery. A later `@NAME@_esp_bus` for an
  open port with another cfg is ARG.
  With `install` false the bus uses a port another part of the firmware
  installed (a codec driver on a shared bus) and recovers nothing itself:
  that driver resets on its own timeouts, and taking the lines from under
  it would break its transaction. Taking another part's bus needs IDF 5.4
  on the new driver (`i2c_master_get_bus_handle`). On IDF 5.x the legacy
  and new drivers can't be linked into one image (it aborts at boot,
  "CONFLICT! driver_ng"), so with `install` false the port's owner must be
  on the new driver too.
- `fuzz/fuzz_@NAME@.c`: every call with any length 0-255, any register and
  any reason-buffer size, on a mock bus whose faults come from the input,
  with canaries around every buffer. The seeds are the malformed lengths
  (0, 33, 255 on read, write and check), a read wrapping past 0xff with a
  1-byte reason buffer, SDA held 9 clocks and for good, SCL held, and
  NACKs and timeouts past the retries.

What was checked where, honestly:

- The driver core and the recovery sequence: host tests and the fuzzer, on
  the mock.
- `@NAME@_esp.c`: built for IDF 4.4.8 and 5.5.6 by `fw new check`. On a
  board, on IDF 4.4.8's legacy driver with `install` false only: the
  hi-fi lab's `i2c` experiment (a copy of this component) reads a Lite's
  CH224 and ES8388 through it, and `fw hil` checks them against the
  datasheets: 5/5 on Lite 5 (2026-11-17), CH224 0x09 = 0x88 (PD) and
  0x50 = 0x64 (5 A), the ES8388 acked and read back, no NACK or timeout.
  The new-driver path and the recovery have not been run against a held
  line on real hardware.

Rules the lab found:

- On a bus shared with a driver you don't own (squeezelite's codec driver
  on the Lite), only read, and only between its transactions. A HIL check
  of a product board reads ID and status registers and writes nothing.
- Check only what the datasheet's table describes. The first plan held
  the CH224's undescribed status bit 7 to 0; a Lite on PD reads 0x88
  (bit 7 set), so it is now reported and not checked.
- A CH224's register 0x0A is write-only and selects the PD voltage: a
  stray write to it can put 20 V on the board. Never write a power chip's
  registers from a test.
- Recovery on the legacy driver re-routes the pins: anything else on the
  same port must be idle meanwhile (callers serialise).
