#include <stdio.h>

#include "rr_boot.h"

rr_trial_verdict_t rr_trial_judge(const rr_trial_t *t, uint32_t elapsed_s, char *why, size_t n) {
	const char *missing = NULL;
	if (!t->component_ok) {
		snprintf(why, n, "the rr component didn't start");
		return RR_TRIAL_BAD; /* it won't start later either */
	}
	if (t->forced_fail) {
		snprintf(why, n, "built with RR_SELFTEST_FAIL");
		return RR_TRIAL_BAD;
	}
	if (t->no_key) {
		snprintf(why, n, "no Hive key in this build");
		return RR_TRIAL_BAD; /* keep the image that can still take commands */
	}
	if (!t->heap_ok) missing = "heap below 12 KB";
	else if (t->wifi_configured && !t->wifi_ok) missing = "Wi-Fi not joined";
	else if (t->relay_configured && !t->relay_ok) missing = "relay not reached";
	if (!missing) {
		snprintf(why, n, "all checks passed in %u s", (unsigned) elapsed_s);
		return RR_TRIAL_GOOD;
	}
	if (elapsed_s >= RR_TRIAL_LIMIT_S) {
		snprintf(why, n, "%s after %u s", missing, (unsigned) elapsed_s);
		return RR_TRIAL_BAD;
	}
	snprintf(why, n, "%s", missing);
	return RR_TRIAL_WAIT;
}

bool rr_heap_boot_due(uint32_t up_s, bool net_up, bool relay_configured, bool relay_up) {
	if (up_s >= RR_HEAP_BOOT_LATEST_S) return true;
	return up_s >= RR_HEAP_BOOT_AFTER_S && net_up && (!relay_configured || relay_up);
}

bool rr_heap_line(uint32_t now_free, uint32_t boot_free, char *out, size_t n) {
	if (boot_free)
		snprintf(out, n, "%u B free, %u B at boot, %u %s the %u floor", (unsigned) now_free, (unsigned) boot_free,
			 (unsigned) (boot_free >= RR_HEAP_MIN ? boot_free - RR_HEAP_MIN : RR_HEAP_MIN - boot_free),
			 boot_free >= RR_HEAP_MIN ? "over" : "under", (unsigned) RR_HEAP_MIN);
	else
		snprintf(out, n, "%u B free, boot not measured yet (floor %u)", (unsigned) now_free,
			 (unsigned) RR_HEAP_MIN);
	return now_free > RR_HEAP_MIN;
}
