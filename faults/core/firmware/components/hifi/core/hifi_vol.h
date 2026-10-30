/*
 * The receiver's one volume: 0-100, mute and a maximum, kept the same
 * whichever source plays. Every source (LMS, AirPlay, Bluetooth, Spotify)
 * sets squeezelite's output gain through set_volume(); our patch hands that
 * gain here first, so the volume follows the source's slider, the maximum
 * caps it and mute holds it at zero. The app, the buttons when nothing
 * streams, and the remote set it here directly.
 *
 * Gains are squeezelite's 16.16 fixed point (65536 = 0 dB). The curve is
 * gain = (volume / 100)^3, the one squeezelite already uses for AirPlay and
 * Bluetooth, so their sliders read back as the same number here.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define HIFI_VOL_UNITY 65536u
#define HIFI_VOL_MAX_MIN 30 /* the lowest maximum the app can set */
#define HIFI_VOL_STEP 5     /* a button or remote press */
#define HIFI_VOL_KNOB_STEP 2 /* a knob detent: 50 clicks from silent to full */

typedef struct {
	int volume;     /* 0..max_volume */
	int max_volume; /* HIFI_VOL_MAX_MIN..100 */
	bool mute;
	uint32_t gain;  /* what the volume asks for, before mute: a source's own
			 * gain when it set it, so its curve is kept exactly */
} hifi_vol_t;

void hifi_vol_init(hifi_vol_t *v, int volume, int max_volume);

uint32_t hifi_vol_to_gain(int volume);
int hifi_vol_from_gain(uint32_t gain);

/* A source asked for `gain`: returns the gain to apply. */
uint32_t hifi_vol_source(hifi_vol_t *v, uint32_t gain);

/* The app. -1 leaves a field as it is; the maximum is applied first, then the
 * volume, which is held to it. Returns false (and changes nothing) if a value
 * is out of range. */
bool hifi_vol_set(hifi_vol_t *v, int volume, int mute, int max_volume);

/* A button or remote step (+1 up, -1 down): HIFI_VOL_STEP, and unmutes. */
void hifi_vol_step(hifi_vol_t *v, int dir);

/* A knob turn of `detents` (+ clockwise): HIFI_VOL_KNOB_STEP each, and unmutes. */
void hifi_vol_turn(hifi_vol_t *v, int detents);

/* The gain to apply now: 0 when muted. */
uint32_t hifi_vol_gain(const hifi_vol_t *v);

/*
 * For an analog path that skips squeezelite (Lite's DJ direct: the codec's
 * bypass): the codec output-volume code for this volume, in 1.5 dB steps
 * below `full_code` (the code at volume 100), never above it, never below 0.
 * -1 means mute the path.
 */
int hifi_vol_codec_code(const hifi_vol_t *v, int full_code);
