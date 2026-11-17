/*
 * A new firmware image boots on trial: the bootloader marks it
 * pending_verify, and unless the app marks it good it rolls back to the
 * previous one. This judges the trial from what the board sees; rr_relay.c
 * marks the image good (esp_ota_mark_app_valid_cancel_rollback) or bad and
 * restarts into the previous one. Add your app's own checks to rr_trial_t.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RR_TRIAL_LIMIT_S 120
/* internal RAM still free for a board to count as healthy (self-test and
 * trial). A live TLS link to the bench holds about 4 KB of it. */
#define RR_HEAP_MIN 12000

typedef struct {
	bool component_ok;    /* our init ran */
	bool wifi_configured, wifi_ok;
	bool relay_configured, relay_ok;
	bool heap_ok;
	bool forced_fail;     /* CONFIG_RR_SELFTEST_FAIL: a build that fails on purpose */
} rr_trial_t;

typedef enum { RR_TRIAL_WAIT = 0, RR_TRIAL_GOOD, RR_TRIAL_BAD } rr_trial_verdict_t;

/* why gets the first thing still missing (or why it failed) */
rr_trial_verdict_t rr_trial_judge(const rr_trial_t *t, uint32_t elapsed_s, char *why, size_t n);

/* The boot heap: internal RAM free once the board has settled, the figure
 * that must stay well over RR_HEAP_MIN (16000 B is the aim, so one more
 * feature can't roll a board back). Settled means the network and the
 * bench link (when set up) are up and 30 s have passed, or 90 s in any
 * case. */
#define RR_HEAP_BOOT_AFTER_S 30
#define RR_HEAP_BOOT_LATEST_S 90
bool rr_heap_boot_due(uint32_t up_s, bool net_up, bool relay_configured, bool relay_up);

/* the self-test's heap line into out (64 B is enough): free now, at boot
 * and the boot figure's margin over the floor. boot 0: not measured yet.
 * Returns the check's verdict, which is free now over the floor. */
bool rr_heap_line(uint32_t now_free, uint32_t boot_free, char *out, size_t n);
