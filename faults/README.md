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

- `lite/`: the hifi receiver's Lite firmware (hifi-receiver-2 main a7f32da:
  `firmware/` files the checks read, and `boards/lite-main/kicad/lite-main.net`).
  `sizes.json` holds its images' sizes from the 2026-10-28 build that
  `partitions.csv`'s header records (recovery 1.52 MB, main app 2.77 MB).
- `core/`: the Plus firmware on the core board (hifi-receiver-2 main ac484b1:
  `firmware/` files the hifi-plus target's constraints read, and
  `boards/core/kicad/core.net`): the Sabre DAC, the line-in ADC, the sub
  DAC, the PD sink and the I/O expander, for the audio path's checks.

The bases are copies, so the score never depends on another project's
clone. When the product's firmware or board moves on, refresh a base by
copying the same files from its project's main, check `fw constraints faults/<base>`
finds no problem, and fix any fault whose `find` no longer matches.
