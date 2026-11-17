#include "@NAME@.h"

uint32_t @NAME@_scale(int pct, uint32_t max) {
	if (pct <= 0) return 0;
	if (pct >= 100) return max;
	return (uint32_t) (((uint64_t) max * (uint32_t) pct + 50) / 100);
}
