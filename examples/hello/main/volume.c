#include "volume.h"

uint8_t volume_to_attenuation(int percent)
{
    if (percent <= 0) {
        return 192;
    }
    if (percent >= 100) {
        return 0;
    }
    /* Linear in dB over the top 60 dB (0-120 steps); 0% is mute. */
    return (uint8_t)(120 - (percent * 120) / 100);
}
