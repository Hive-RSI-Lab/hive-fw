#pragma once
#include <stdint.h>

/* Map a 0-100 user volume to a codec attenuation register value, 0 (loudest)
 * to 192 (-96 dB in 0.5 dB steps, the ES8388 DAC volume scale). Plain C with
 * no IDF calls, so `fw host-test` runs it on the host. */
uint8_t volume_to_attenuation(int percent);
