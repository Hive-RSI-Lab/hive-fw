# plain: an empty component split into host-tested C and an IDF side

`@NAME@.c` holds the decisions in plain C (no IDF headers), so
`test/host/test_@NAME@.c` runs them on the build machine; `@NAME@_esp.c`
holds the IDF and driver calls and is built only for the board. Replace
`@NAME@_scale` with the component's own logic and keep the split.
