/*
 * The core board's front controls through its I2C I/O expander (TCA9555,
 * or the TCA9539 the first-spin review swaps in; both have the same
 * registers): an EC11 knob on two inputs and push buttons on others, all
 * pulled up, so a pressed button or a closed encoder contact reads 0.
 *
 * The board code reads the expander's two input ports whenever its INT
 * line falls and every 10 ms besides, and hands the 16 bits here; this
 * turns them into knob detents and button presses. Plain logic, so the
 * host tests run it.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define HIFI_EXP_BUTTONS 4
#define HIFI_EXP_DEBOUNCE_MS 30
#define HIFI_EXP_LONG_MS 1000

typedef enum {
	HIFI_EXP_KNOB,  /* detents: +1 clockwise, -1 counter-clockwise */
	HIFI_EXP_PRESS, /* a button released before HIFI_EXP_LONG_MS */
	HIFI_EXP_LONG,  /* a button held HIFI_EXP_LONG_MS (once per hold) */
} hifi_exp_kind_t;

typedef struct {
	hifi_exp_kind_t kind;
	int button; /* index into the config's buttons; the knob: 0 */
	int steps;  /* the knob's detents */
} hifi_exp_event_t;

typedef struct {
	int enc_a, enc_b;          /* input bits (0-15), -1: no knob */
	int edges_per_detent;      /* EC11: 4 (one full quadrature cycle a click) */
	int buttons[HIFI_EXP_BUTTONS]; /* input bits, -1: none */
} hifi_exp_cfg_t;

typedef struct {
	hifi_exp_cfg_t cfg;
	bool started;
	int ab;            /* last knob state, (A << 1) | B */
	int edges;         /* edges since the last detent */
	bool raw[HIFI_EXP_BUTTONS], down[HIFI_EXP_BUTTONS], long_sent[HIFI_EXP_BUTTONS];
	uint32_t t_raw[HIFI_EXP_BUTTONS], t_down[HIFI_EXP_BUTTONS];
} hifi_exp_t;

void hifi_exp_init(hifi_exp_t *e, const hifi_exp_cfg_t *cfg);

/*
 * The expander's inputs as read at now_ms (bit n = Pn for n < 8, P1(n-8)
 * above). Writes up to `max` events; returns how many. A knob turn gives
 * one event with the detents it made since the last call.
 */
int hifi_exp_input(hifi_exp_t *e, uint16_t pins, uint32_t now_ms, hifi_exp_event_t *ev, int max);

/* An output port's new value with `bit` (0-7) set to `on`. */
static inline uint8_t hifi_exp_bit(uint8_t port, int bit, bool on) {
	return on ? (uint8_t) (port | 1u << bit) : (uint8_t) (port & ~(1u << bit));
}
