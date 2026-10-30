# The fault corpus

What `fw score` counts. Each `faults/<base>/` is a copy of a real
firmware's files and its board's netlist, as `lint.json` lays them out for
`fw lint`, and `faults.json`, the faults planted on it: each an `id`, the
check it's for (`expect`), what it is (`about`) and `edits`, find/replace
pairs on the base's files (each `find` must occur exactly once).

- `lite/`: the hifi receiver's Lite firmware (hifi-receiver-2 main a7f32da:
  `firmware/` files the checks read, and `boards/lite-main/kicad/lite-main.net`).
  `sizes.json` holds its images' sizes from the 2026-10-28 build that
  `partitions.csv`'s header records (recovery 1.52 MB, main app 2.77 MB).

The bases are copies, so the score never depends on another project's
clone. When the product's firmware or board moves on, refresh a base by
copying the same files from its project's main, check `fw lint faults/<base>`
is still clean, and fix any fault whose `find` no longer matches.
