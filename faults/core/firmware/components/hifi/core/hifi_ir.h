/*
 * Learned IR remote codes. squeezelite-esp32 decodes NEC (or RC5) on the IR
 * receiver's pin and hands each frame, repeats included, to its ir_handler;
 * our patch shows it to hifi_ir_code() first. A code learned for one of the
 * receiver's actions is ours; any other code goes on to squeezelite as
 * before (LMS's own remote map, its default codes).
 *
 * A code is (address << 16) | command, as squeezelite's own IR map has it.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
	HIFI_IR_VOLUP, HIFI_IR_VOLDOWN, HIFI_IR_MUTE, HIFI_IR_TOGGLE,
	HIFI_IR_NEXT, HIFI_IR_PREV, HIFI_IR_INPUT, HIFI_IR_POWER,
	HIFI_IR_N
} hifi_ir_action_t;

#define HIFI_IR_LEARN_MS 30000 /* learning gives up after this */
#define HIFI_IR_REPEAT_MS 250  /* frames closer than this are a held button */
#define HIFI_IR_HOLD_STEP_MS 200 /* a held volume button steps this often */

/* hifi_ir_code() results besides an action */
#define HIFI_IR_PASS (-1)  /* not ours: hand it to squeezelite */
#define HIFI_IR_EATEN (-2) /* ours, nothing to do (a repeat, or just learned) */

typedef struct {
	uint32_t code[HIFI_IR_N];
	bool have[HIFI_IR_N];
	int learning; /* an action, or -1 */
	uint32_t learn_since_ms;
	bool have_last;
	uint32_t last_code, last_ms, fired_ms;
} hifi_ir_t;

const char *hifi_ir_action_name(int action);
int hifi_ir_action_from(const char *name); /* -1 if none */

void hifi_ir_init(hifi_ir_t *ir);
void hifi_ir_learn(hifi_ir_t *ir, int action, uint32_t now_ms);
/* action -1 forgets them all; also ends any learning */
void hifi_ir_forget(hifi_ir_t *ir, int action);
/* the action being learned, or -1 (after HIFI_IR_LEARN_MS it gives up) */
int hifi_ir_learning(hifi_ir_t *ir, uint32_t now_ms);

/*
 * A frame came in. Returns the action to do, HIFI_IR_EATEN or HIFI_IR_PASS.
 * *changed is set when the learned table changed (save it).
 */
int hifi_ir_code(hifi_ir_t *ir, uint32_t code, uint32_t now_ms, bool *changed);

/* The learned table as text for NVS, "volup=00ff40bf,mute=..." */
void hifi_ir_save(const hifi_ir_t *ir, char *out, size_t cap);
void hifi_ir_load(hifi_ir_t *ir, const char *s);
