/*
 * Power governor: keeps the box's input power under 90% of what the USB-C
 * contract allows, so a charger never trips on a long loud passage.
 *
 * On Lite there is no current sensor, so input power is estimated from the
 * signal going to the DAC: the 1 s mean square of each channel, scaled by
 * the analog chain (codec full scale, codec and amp gain) into the speaker
 * load, capped at the amp's clipping power for the supply, divided by the
 * amp's efficiency, plus the electronics' idle draw. When the 1 s average
 * would pass the budget, a gain below 1 is applied to the samples (fast
 * attack, slow release) so the average sits on the budget.
 *
 * It runs on the samples after squeezelite's volume, equalizer and loudness,
 * just before I2S (the patch calls it at the end of equalizer_process), so it
 * sees what the amp will get from every source (LMS, AirPlay, Spotify,
 * Bluetooth, test tones), EQ boost included.
 * Plus/Ultra have an INA180 on VBUS; their firmware will correct the
 * estimate with the measured current (plan item 18).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
	float vfs_peak_v;  /* amp output peak volts for a 0 dBFS sine */
	float swing;       /* output peak / supply at clipping */
	float r_load;      /* ohms assumed per channel */
	float efficiency;  /* amp, at power */
	float p_idle_w;    /* everything else on the board */
	float budget_frac; /* of the contract's power */
	float tau_s;       /* power averaging */
	float attack_s, release_s;
	float min_gain;    /* never attenuate more than this (linear) */
	/* the charger's current limit acts on milliseconds, and the PVDD bulk
	 * can't carry a bass half-cycle: the input over one audio block (~5-20
	 * ms) is held at or under peak_frac of the contract. Its peak decays
	 * over peak_hold_s, so the gain doesn't follow the bass waveform */
	float peak_frac;
	float peak_hold_s;
	/* thermal limit: speaker power of one stereo amp, both channels, averaged
	 * over thermal_tau_s; 0 = none. For an amp the board cools without a
	 * heatsink, whose continuous power is below what the supply allows */
	float thermal_out_w;
	float thermal_out2_w; /* the same with two amps playing (total) */
	float thermal_tau_s;
} hifi_gov_cfg_t;

typedef struct {
	hifi_gov_cfg_t cfg;
	float vsupply, budget_w, contract_w;
	float p_out_pre;   /* averaged speaker power before the limiter */
	float p_out_slow;  /* the same over thermal_tau_s */
	float p_out_peak;  /* the loudest block's power, decaying over peak_hold_s */
	float ms_avg;      /* averaged mean square, both channels, for the level */
	float gain;        /* applied now */
	float target;
	float trim;        /* a ceiling on the gain from outside (an amp's OTW): 1 none */
	bool limiting;
	uint32_t events;
	int amps;          /* stereo amps playing the program: Ultra's zone B makes 2 */
} hifi_gov_t;

/* lite-main: ES8388 (~0.9 Vrms full scale, -3 dB cap) into a 26 dB TPA3118 */
void hifi_gov_cfg_lite(hifi_gov_cfg_t *cfg);
void hifi_gov_cfg_core(hifi_gov_cfg_t *cfg);
void hifi_gov_init(hifi_gov_t *g, const hifi_gov_cfg_t *cfg);
/* contract_w: what the source offers; the budget is budget_frac of it */
void hifi_gov_set_supply(hifi_gov_t *g, float vsupply, float contract_w);

/* Processes interleaved stereo in place. bytes_per_frame 4 (s16) or 8 (s32). */
void hifi_gov_process(hifi_gov_t *g, void *buf, size_t frames, int bytes_per_frame, unsigned rate);

float hifi_gov_gain_db(const hifi_gov_t *g);
float hifi_gov_in_w(const hifi_gov_t *g);   /* estimated input power now */
float hifi_gov_out_w(const hifi_gov_t *g);  /* estimated speaker power now */
float hifi_gov_level_dbfs(const hifi_gov_t *g); /* 1 s RMS vs a full-scale sine */
