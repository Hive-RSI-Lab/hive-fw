/*
 * @NAME@: say what it does here, in a sentence or two.
 *
 * Keep the decisions in plain C (this file and @NAME@.c, no IDF calls) so
 * the host test runs them; put the IDF and driver calls in @NAME@_esp.c,
 * which only the board build compiles.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* an example: a 0-100 percentage clamped and scaled to 0..max */
uint32_t @NAME@_scale(int pct, uint32_t max);
