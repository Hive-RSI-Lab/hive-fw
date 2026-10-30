/*
 * Test signals for bring-up: sine, log sweep, pink noise, silence. Rendered
 * as interleaved stereo s16 with TPDF dither and 10 ms fades at both ends
 * so starting or stopping one never clicks.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
	HIFI_TONE_NONE = 0,
	HIFI_TONE_SINE,
	HIFI_TONE_SWEEP,
	HIFI_TONE_NOISE,
	HIFI_TONE_SILENCE,
} hifi_tone_type_t;

typedef enum { HIFI_CH_BOTH = 0, HIFI_CH_LEFT, HIFI_CH_RIGHT } hifi_channel_t;

typedef struct {
	hifi_tone_type_t type;
	float freq_hz;            /* sine */
	float start_hz, end_hz;   /* sweep */
	float level_dbfs;         /* sine/sweep peak; noise RMS, both vs a full-scale sine */
	float duration_s;
	hifi_channel_t channel;
} hifi_tone_params_t;

typedef struct {
	hifi_tone_params_t p;
	unsigned rate;
	uint64_t n, total, fade;
	double phase;
	float amp;
	double k_sweep;           /* ln(end/start) / duration */
	uint32_t rng;
	float b[7];               /* pink filter state */
	float pink_scale;
} hifi_tone_t;

const char *hifi_tone_type_name(hifi_tone_type_t t);
hifi_tone_type_t hifi_tone_type_from(const char *s);
const char *hifi_channel_name(hifi_channel_t c);

/* Limits the API enforces; returns NULL if valid, else what's wrong. */
const char *hifi_tone_check(const hifi_tone_params_t *p, bool force);

void hifi_tone_start(hifi_tone_t *t, const hifi_tone_params_t *p, unsigned rate);
/* Renders up to `frames`; returns how many (0 once finished). */
size_t hifi_tone_render(hifi_tone_t *t, int16_t *out, size_t frames);
bool hifi_tone_done(const hifi_tone_t *t);
/* Starts the 10 ms fade-out now; the tone ends after it. */
void hifi_tone_stop(hifi_tone_t *t);
