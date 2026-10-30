/*
 * What is playing, for /state's audio.playing. squeezelite-esp32's AirPlay,
 * Bluetooth and Spotify sinks tell its displayer when they take over, play,
 * pause, stop, what the track is and how far in it is; our patch passes the
 * same calls here, display or not. LMS sends its track text only as display
 * graphics, so for LMS the state is known and the text isn't.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { HIFI_PLAY_NONE = 0, HIFI_PLAY_STOP, HIFI_PLAY_PLAY, HIFI_PLAY_PAUSE } hifi_play_t;

typedef struct {
	hifi_play_t state;
	char title[96], artist[64], album[64]; /* "" when unknown */
	int32_t elapsed_ms;  /* at tick_ms; -1 unknown */
	int32_t duration_ms; /* -1 unknown */
	uint32_t tick_ms;
} hifi_meta_t;

const char *hifi_play_name(hifi_play_t s); /* "stop" "play" "pause", NULL for none */

void hifi_meta_init(hifi_meta_t *m);
void hifi_meta_active(hifi_meta_t *m);           /* a source took over: nothing known yet */
void hifi_meta_idle(hifi_meta_t *m);             /* it let go */
void hifi_meta_play(hifi_meta_t *m, uint32_t now_ms);
void hifi_meta_pause(hifi_meta_t *m, uint32_t now_ms);
/* NULLs are unknown; text is cut at a whole UTF-8 character */
void hifi_meta_track(hifi_meta_t *m, const char *artist, const char *album, const char *title);
/* elapsed < 0 leaves it, duration <= 0 leaves it */
void hifi_meta_timer(hifi_meta_t *m, int32_t elapsed_ms, int32_t duration_ms, uint32_t now_ms);
/* elapsed now (counting on while playing, held at the duration), -1 unknown */
int32_t hifi_meta_elapsed_ms(const hifi_meta_t *m, uint32_t now_ms);

/* copies src into dst (cap bytes) cutting at a UTF-8 character boundary */
void hifi_utf8_copy(char *dst, size_t cap, const char *src);
