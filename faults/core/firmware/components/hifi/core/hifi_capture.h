/*
 * The board records its own outputs (docs/remote.md, "The board measures its
 * own output"): the codec's ADC on the loopback taps or the RCA input, as
 * 16-bit stereo. The board code fills the samples; this turns them into the
 * API's reply.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define HIFI_CAPTURE_FRAMES_MAX 16384
#define HIFI_CAPTURE_FRAMES_DEFAULT 4096

typedef enum { HIFI_CAP_LOOPBACK = 0, HIFI_CAP_LINE } hifi_cap_src_t;

typedef struct {
	int rate;
	const char *ch[2];  /* "spk_l+", "spk_r+" or "line_l", "line_r" */
	float v_per_fs[2];  /* peak volts at the tap's source for an ADC full scale; NaN if unknown */
} hifi_cap_info_t;

typedef struct {
	float rms_fs, peak_fs, dc_fs; /* relative to full scale */
	int clipped;                  /* samples at the ADC's limits */
} hifi_cap_stats_t;

/* Stats of one channel (0 or 1) of interleaved stereo. */
void hifi_capture_stats(const int16_t *pcm, int frames, int ch, hifi_cap_stats_t *out);
