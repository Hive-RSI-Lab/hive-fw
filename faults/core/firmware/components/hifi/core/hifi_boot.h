/*
 * A new firmware image boots on trial (docs/remote.md, "Rollback"). This
 * judges the trial from what the board sees; the board marks the image good
 * (esp_ota_mark_app_valid_cancel_rollback) or bad and restarts into the
 * previous one.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HIFI_TRIAL_LIMIT_S 120

typedef struct {
	bool component_ok;    /* our init ran */
	bool wifi_configured, wifi_ok;
	bool relay_configured, relay_ok;
	bool i2c_ok;          /* the codec and the PD sink answer */
	bool heap_ok;
} hifi_trial_t;

typedef enum { HIFI_TRIAL_WAIT = 0, HIFI_TRIAL_GOOD, HIFI_TRIAL_BAD } hifi_trial_verdict_t;

/* why gets the first thing still missing (or why it failed) */
hifi_trial_verdict_t hifi_trial_judge(const hifi_trial_t *t, uint32_t elapsed_s, char *why, size_t n);
