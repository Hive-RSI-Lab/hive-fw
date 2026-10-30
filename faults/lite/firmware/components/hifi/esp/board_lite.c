/*
 * lite-main (hifi_board_lite.h): the CH224A's PG and the amp's FAULTZ on
 * GPIOs, VBUS through a divider on the ADC, PVDD_EN and the TPA3118's SDZ
 * on GPIOs, and the ES8388's input routing, sub level and DJ-direct volume.
 */
#include <math.h>
#include <stdlib.h>

#include "driver/adc.h"
#include "driver/gpio.h"
#include "esp_adc_cal.h"
#include "esp_heap_caps.h"

#include "hifi_board.h"
#include "hifi_bsp.h"
#include "hifi_esp.h"
#include "hifi_pd.h"

static esp_adc_cal_characteristics_t adc_chars;
static uint32_t last_pd;

void hifi_bsp_init(hifi_power_cfg_t *pc, hifi_gov_cfg_t *gc) {
	gpio_config_t out = {.pin_bit_mask = BIT64(PIN_PVDD_EN) | BIT64(PIN_AMP_SDZ), .mode = GPIO_MODE_OUTPUT};
	gpio_set_level(PIN_PVDD_EN, 0);
	gpio_set_level(PIN_AMP_SDZ, 0);
	gpio_config(&out);
	gpio_config_t in = {.pin_bit_mask = BIT64(PIN_PD_PG) | BIT64(PIN_AMP_FAULTZ), .mode = GPIO_MODE_INPUT,
			    .pull_up_en = GPIO_PULLUP_ENABLE};
	gpio_config(&in);

	adc1_config_width(ADC_WIDTH_BIT_12);
	adc1_config_channel_atten(ADC_VBUS, ADC_ATTEN_DB_12);
	adc1_config_channel_atten(ADC_PVDD, ADC_ATTEN_DB_12);
	esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_12, ADC_WIDTH_BIT_12, 1100, &adc_chars);

	hifi_power_cfg_lite(pc);
	hifi_gov_cfg_lite(gc);
}

void hifi_bsp_start(void) {}

static float read_div(adc1_channel_t ch, float divider) {
	uint32_t sum = 0;
	for (int i = 0; i < 8; i++) sum += adc1_get_raw(ch);
	return esp_adc_cal_raw_to_voltage(sum / 8, &adc_chars) * divider / 1000.0f;
}

void hifi_bsp_power_in(uint32_t now, hifi_bsp_power_t *p) {
	p->vbus_v = read_div(ADC_VBUS, VBUS_DIVIDER);
	/* PVDD after the switch (PVDD_SENSE): the sequencer confirms it closed */
	p->pvdd_v = read_div(ADC_PVDD, PVDD_DIVIDER);
	p->pg = gpio_get_level(PIN_PD_PG) == 0;
	p->fault = gpio_get_level(PIN_AMP_FAULTZ) == 0;
	p->vbus_i_a = NAN;
	p->otw = false; /* the TPA3118 has no warning pin, only FAULTZ */
	/* the PD sink's registers every 500 ms */
	p->pd_read = now - last_pd >= 500;
	if (p->pd_read) {
		last_pd = now;
		hifi_i2c_ensure();
		p->sink_ok = hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_STATUS, &p->pd_status) == ESP_OK &&
			     hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_CURRENT, &p->pd_current) == ESP_OK;
		if (!p->sink_ok) p->pd_current = 0;
	}
}

void hifi_bsp_power_out(const hifi_power_out_t *o) {
	if (!o->amp_on) gpio_set_level(PIN_AMP_SDZ, 0);
	gpio_set_level(PIN_PVDD_EN, o->pvdd_en);
	if (o->amp_on) gpio_set_level(PIN_AMP_SDZ, 1);
}

/* ------------------------------------------------------------------ codec */

/* LOUT2/ROUT2 volume code, 1.5 dB a step, 30 = 0 dB. At most 24 (-9 dB):
 * the sub mute's 2N7002s (Q13-Q15) are off with their gates near 0 V, and a
 * part at Vth(min) 1.0 V leaks on the line's negative peaks once warm (0.1-
 * 0.4 % THD at -6 dB, 50-70 C; 0.014 % at -9 dB: docs/review-4/als.md
 * R4-ALS-1, sims/als/mute_corners.py). Squeezelite's codec init
 * (patches/0001, DAC_CONTROLSET) starts them at 24 too (R4-ALS-2) */
#define SUB_CODE_MAX 24

static int sub_code(float db) {
	int code = (int) lrintf(30 + db / 1.5f);
	return code < 0 ? 0 : code > SUB_CODE_MAX ? SUB_CODE_MAX : code;
}

float hifi_bsp_sub_db(float db) { return (sub_code(db) - 30) * 1.5f; }

/* Input routing and sub level onto the ES8388. Squeezelite's controlset
 * (firmware/patches, Kconfig) sets the rest at codec start. */
static esp_err_t codec_apply(hifi_input_t in, bool on, float sub_db, const hifi_vol_t *vol) {
	int code = sub_code(sub_db);
	/* the bypass skips squeezelite, so the volume goes on the codec's outputs */
	int out1 = hifi_vol_codec_code(vol, ES8388_OUT1_FULL);
	esp_err_t err = ESP_OK;
	if (in == HIFI_INPUT_DJ_DIRECT) {
		/* analog inputs on (ADCs stay off), mixers take LIN1/RIN1 at -3 dB, DAC out
		 * of the mix: the line in reaches the amp and sub outs with no latency.
		 * Muted: the line in leaves the mix too */
		uint8_t mix = ES8388_MIX_LIN_M3DB | (out1 < 0 ? 0 : 0x40); /* bit6: LIN into the mixer */
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_ADCPOWER, 0x38);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_DACCONTROL17, mix);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_DACCONTROL20, mix);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_LOUT1VOL, out1 < 0 ? 0 : out1);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_ROUT1VOL, out1 < 0 ? 0 : out1);
	} else {
		/* squeezelite's own setting: DAC into the mixers, inputs powered down;
		 * the volume is squeezelite's digital gain (hifi_volume_hook) */
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_DACCONTROL17, 0xB8);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_DACCONTROL20, 0xB8);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_ADCPOWER, 0xFC);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_LOUT1VOL, ES8388_OUT1_FULL);
		err |= hifi_i2c_write(ES8388_ADDR, ES8388_ROUT1VOL, ES8388_OUT1_FULL);
	}
	err |= hifi_i2c_write(ES8388_ADDR, ES8388_DACPOWER, on ? 0x3C : 0x30);
	err |= hifi_i2c_write(ES8388_ADDR, ES8388_LOUT2VOL, on ? code : 0);
	err |= hifi_i2c_write(ES8388_ADDR, ES8388_ROUT2VOL, on ? code : 0);
	return err;
}

bool hifi_bsp_route(hifi_input_t in, bool sub_on, float sub_db, const hifi_vol_t *vol, uint32_t now) {
	static uint32_t codec_ok_at;
	uint8_t dummy;
	/* re-apply our routing once the codec answers, and on request */
	if (hifi_i2c_read(ES8388_ADDR, 0x00, &dummy) != ESP_OK) return false;
	if (!codec_ok_at) codec_ok_at = now; /* let squeezelite finish its init */
	return now - codec_ok_at > 2000 && codec_apply(in, sub_on, sub_db, vol) == ESP_OK;
}

/* ------------------------------------------------------------------ info */

void hifi_bsp_info(hifi_info_t *i) {
	i->psram_mb = heap_caps_get_total_size(MALLOC_CAP_SPIRAM) ? 8 : 0; /* WROVER-E N8R8 */
	i->caps = (hifi_caps_t) {
		.dj_direct = true, .eq_min_db = -12, .eq_max_db = 12,
		.sub_count = 2, .sub_crossover = false, .sub_per_sub = false,
		.zones = 1, .eth = false, .pdo_v_max = 20,
	};
}

void hifi_bsp_snapshot(hifi_snapshot_t *s) {
	s->vbus_i_a = s->amp_temp_c = NAN; /* no current or temperature sense on lite-main; PVDD is read */
	s->zones = 1;
}

void hifi_bsp_selftest(hifi_selftest_t *t) {
	uint8_t v;
	hifi_snapshot_t *s = calloc(1, sizeof(*s));
	if (s) {
		hifi_ops.snapshot(s);
		bool vm = fabsf(s->vbus_v - s->contract.v) <= 0.08f * s->contract.v;
		hifi_selftest_add(t, "vbus_matches", vm, "%.2f V", s->vbus_v);
		free(s);
	}
	bool codec = hifi_i2c_read(ES8388_ADDR, 0x00, &v) == ESP_OK;
	hifi_selftest_add(t, "codec_i2c", codec, codec ? "0x%02x acks" : "0x%02x doesn't answer", ES8388_ADDR);
}

bool hifi_bsp_parts_ok(void) {
	uint8_t v;
	return hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_STATUS, &v) == ESP_OK && hifi_i2c_read(ES8388_ADDR, 0x00, &v) == ESP_OK;
}

void hifi_bsp_set_zone(bool on) {}

void hifi_bsp_audio(const uint8_t *buf, uint32_t frames, int bytes_per_frame) {}
