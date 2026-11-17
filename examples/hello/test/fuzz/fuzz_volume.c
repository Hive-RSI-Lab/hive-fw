// fw fuzz's proof on the example: any percent maps to a register value in
// range, and a louder percent never attenuates more.
// fw-src: main/volume.c
// fw-inc: main
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "volume.h"

#define FUZZ_ASSERT(c) do { if (!(c)) { fprintf(stderr, "FUZZ_ASSERT %s:%d %s\n", \
    __FILE__, __LINE__, #c); abort(); } } while (0)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    int32_t p;
    if (size < sizeof p) {
        return 0;
    }
    memcpy(&p, data, sizeof p);
    uint8_t a = volume_to_attenuation(p);
    FUZZ_ASSERT(a <= 192);
    if (p < INT32_MAX) {
        FUZZ_ASSERT(volume_to_attenuation(p + 1) <= a);
    }
    return 0;
}
