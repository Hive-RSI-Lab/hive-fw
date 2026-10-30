# The fault corpus

What `fw score` counts. Each `faults/<base>/` is a copy of a real
firmware's files and its board's netlist, as `fw.json` lays them out for
`fw constraints` (fw's one checker), and `faults.json`, the faults planted
on it: each an `id`, the check it's for (`expect`), what it is (`about`)
and `edits`, find/replace pairs on the base's files (each `find` must occur
exactly once), and its `site`, the signal, setting or partition it breaks.

Each fault is a distinct real mistake: at most one per kind (`expect`) and
site. A second fault of the same kind at the same site is a near-duplicate:
`fw score` prints it as DUPLICATE and counts nothing for it, and the test
fails on it, so restating a fault can't raise the score.

A fault with `blind` was written by someone who hadn't read fw's checker,
its tests or the other faults, from the base's files and Espressif's
documents alone (`blind` names the document and section that says it's a
mistake; its `expect` is the author's name for the rule). The rest were
written beside the check that catches them, so they never show a miss;
blind ones do. `fw score` prints the blind faults it misses last, as the to-do
list: each is a check fw doesn't have yet and a point when it lands (a miss
costs nothing). When a check lands, give it the fault's `expect` as its code,
or set `expect` to the code that catches it. A new batch of blind faults is
best written by an agent told not to read `scripts/`, `tests/`, `docs/`
or any `faults.json`; drop any that restate a fault the base already has.

- `lite/`: the hifi receiver's Lite firmware (hifi-receiver-2 main a7f32da:
  `firmware/` files the checks read, and `boards/lite-main/kicad/lite-main.net`).
  `sizes.json` holds its images' sizes from the 2026-10-28 build that
  `partitions.csv`'s header records (recovery 1.52 MB, main app 2.77 MB).
- `core/`: the Plus firmware on the core board (hifi-receiver-2 main ac484b1:
  `firmware/` files the hifi-plus target's constraints read, and
  `boards/core/kicad/core.net`): the Sabre DAC, the line-in ADC, the sub
  DAC, the PD sink and the I/O expander, for the audio path's checks; and
  faults real to that board elsewhere: the ESP32's pins against the FFC's
  LCD lines and the RMII, the straps its pulls set, the WROVER's PSRAM and
  flash, the table, and the TCA9539 (U17) that carries the TPA3220s' resets
  and flags, PVDD_EN and the ultra-front backlight (EXP-NET, EXP-ADDR,
  EXP-DIR). Ultra builds the same pins and patch lines (HIFI_PLUS ||
  HIFI_ULTRA), so one base covers both.

The bases are copies, so the score never depends on another project's
clone. When the product's firmware or board moves on, refresh a base by
copying the same files from its project's main, check `fw constraints faults/<base>`
finds no problem, and fix any fault whose `find` no longer matches.
