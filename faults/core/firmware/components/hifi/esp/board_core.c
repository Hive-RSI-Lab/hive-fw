/*
 * The core board, Plus and Ultra (hifi_board_core.h). The ESP32 has no pins
 * left, so the power path and the front controls sit on the I2C I/O
 * expander: PVDD_EN, both amps' resets and their wired-OR FAULT and OTW,
 * the DAC's reset, the Ethernet clock, the DJ-direct switch, the LED, and
 * the knob and buttons. The CH224Q's contract is what this code asks it
 * for (there is no VBUS divider to read), the INA186 gives the input
 * current, and the PCM5102A sub DAC gets the sum of the mains from I2S1.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/adc.h"
#include "driver/gpio.h"
#include "driver/i2s.h"
#include "esp_adc_cal.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_rom_gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "soc/gpio_periph.h"
#include "soc/gpio_reg.h"
#include "soc/gpio_sig_map.h"
#include "soc/io_mux_reg.h"

#include "platform_config.h"

#include "hifi_board.h"
#include "hifi_bsp.h"
#include "hifi_esp.h"
#include "hifi_exp.h"
#include "hifi_ir.h"
#include "hifi_pd.h"

static const char TAG[] = "hifi_core";

static SemaphoreHandle_t exp_lock;  /* the expander's output shadow */
static uint8_t exp_addr;            /* 0: none answered */
static uint8_t out0;                /* port 0 as last written */
static volatile uint16_t in_pins = 0xFFFF; /* both input ports, as last read */
static TaskHandle_t ctrl_task;

static esp_adc_cal_characteristics_t adc_chars;
static uint32_t last_pd;
static uint8_t vreq_sent = 0xFF;
static float vreq_v;                 /* the voltage asked for, 0 before */
static uint32_t t_pd;                /* when this PD contract came in */
static bool pd_was;
static float cap_v[7], cap_i[7];     /* the source's fixed PDOs */
static int cap_n = -1;               /* -1 not read for this contract */

static volatile bool zone_b;        /* Ultra: zone B's amp on with zone A's */
static bool amps_a, amps_b, resync;

static volatile float sub_gain = 1.0f; /* 0 when the sub is off */
static volatile bool sub_ready;

/* ------------------------------------------------------------------ expander */

static esp_err_t exp_out(uint8_t v) {
	if (!exp_addr) return ESP_ERR_NOT_FOUND;
	esp_err_t err = hifi_i2c_write(exp_addr, EXP_REG_OUT0, v);
	if (err == ESP_OK) out0 = v;
	return err;
}

/* sets one port-0 output; the I2C write only when it changes */
static esp_err_t exp_set(int bit, bool on) {
	xSemaphoreTake(exp_lock, portMAX_DELAY);
	uint8_t v = hifi_exp_bit(out0, bit, on);
	esp_err_t err = v == out0 ? ESP_OK : exp_out(v);
	xSemaphoreGive(exp_lock);
	return err;
}

static bool exp_read(uint16_t *pins) {
	uint8_t p0, p1;
	if (!exp_addr || hifi_i2c_read(exp_addr, EXP_REG_IN0, &p0) != ESP_OK ||
	    hifi_i2c_read(exp_addr, EXP_REG_IN0 + 1, &p1) != ESP_OK)
		return false;
	*pins = (uint16_t) (p1 << 8 | p0);
	return true;
}

static void exp_init(void) {
	static const uint8_t addrs[] = EXP_ADDRS;
	uint8_t v;
	for (int i = 0; i < sizeof(addrs) && !exp_addr; i++)
		if (hifi_i2c_read(addrs[i], EXP_REG_CFG0, &v) == ESP_OK) exp_addr = addrs[i];
	if (!exp_addr) {
		ESP_LOGE(TAG, "no I/O expander answers: no PVDD, amps, DAC or Ethernet");
		return;
	}
	/* the outputs' levels first, then make them outputs: the DAC out of reset
	 * and the Ethernet clock on (the network starts after us), PVDD and the
	 * amps off */
	esp_err_t err = exp_out(hifi_exp_bit(hifi_exp_bit(0, EXP_DAC_EN, true), EXP_PHY_RST, true));
	err |= hifi_i2c_write(exp_addr, EXP_REG_CFG0, EXP_CFG0);
	err |= hifi_i2c_write(exp_addr, EXP_REG_CFG0 + 1, EXP_CFG1);
	ESP_LOGI(TAG, "I/O expander at 0x%02x%s", exp_addr, err ? ", but it didn't take its setup" : "");
}

/* ------------------------------------------------------------------ the Sabre */

/* Squeezelite's 16-bit build sends 16-bit I2S words (BCLK 32 fs); the
 * ES9018K2M comes out of reset expecting 32. Register 1 bits 7:6 are the
 * word length (00: 16 bit); the rest stays as reset left it. */
static esp_err_t dac_init(void) {
	uint8_t r1;
	esp_err_t err = hifi_i2c_read(ES9018_ADDR, 0x01, &r1);
	if (err == ESP_OK) err = hifi_i2c_write(ES9018_ADDR, 0x01, r1 & 0x3F);
	if (err != ESP_OK) ESP_LOGE(TAG, "ES9018K2M didn't take its I2S word length");
	return err;
}

/* ------------------------------------------------------------------ init */

/* Before a software restart (OTA, the API's reboot): the 50 MHz PHY clock
 * off, since a restart doesn't move EN and the expander would keep it on
 * GPIO0, the boot strap (firmware requirement C1, review-3 R3-DIGITAL-1).
 * A brownout or a watchdog reset skips this: the board's EN supervisor
 * covers those */
static void phy_clock_off(void) {
	if (!exp_addr) return;
	exp_out(hifi_exp_bit(hifi_exp_bit(hifi_exp_bit(out0, EXP_PHY_RST, false), EXP_AMP_A_RST, false), EXP_AMP_B_RST, false));
}

void hifi_bsp_init(hifi_power_cfg_t *pc, hifi_gov_cfg_t *gc) {
	exp_lock = xSemaphoreCreateMutex();
	exp_init();
	esp_register_shutdown_handler(phy_clock_off);
	vTaskDelay(pdMS_TO_TICKS(20)); /* the DAC leaves reset, the PHY's clock starts */
	dac_init();

	adc1_config_width(ADC_WIDTH_BIT_12);
	adc1_config_channel_atten(ADC_ISENSE, ADC_ATTEN_DB_6); /* to ~1.75 V: 5 A is 1.25 V */
	esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_6, ADC_WIDTH_BIT_12, 1100, &adc_chars);

	char *s = config_alloc_get_str("hifi_zone_b", NULL, "0");
	zone_b = HIFI_ZONES > 1 && s && !strcmp(s, "1");
	free(s);

	hifi_power_cfg_core(pc);
	hifi_gov_cfg_core(gc);
#if HIFI_SPLIT
	/* one channel per chip: in the vented box at 30 C room each chip holds
	 * ~2.0 W at Tj 110 C, which is 9.6 W of sustained output per channel
	 * into 4 ohm (hwde check_thermal; 14.8 W into 6 ohm): 12 dB-crest music
	 * at the 77 W clip level */
	gc->thermal_out_w = 19.0f;
#endif
}

/* ------------------------------------------------------------------ power */

void hifi_bsp_power_in(uint32_t now, hifi_bsp_power_t *p) {
	uint32_t sum = 0;
	for (int i = 0; i < 8; i++) sum += adc1_get_raw(ADC_ISENSE);
	/* the ADC reads 0 up to about 0.1 V (0.4 A), where its calibration would
	 * still give ~0.1 V: call that 0 A rather than a made-up current */
	p->vbus_i_a = sum / 8 < ISENSE_RAW_FLOOR ? 0.0f
		    : esp_adc_cal_raw_to_voltage(sum / 8, &adc_chars) / 1000.0f / ISENSE_V_PER_A;
	p->fault = !(in_pins >> EXP_AMP_FAULT & 1);
	p->otw = !(in_pins >> EXP_AMP_OTW & 1);
	p->pvdd_v = NAN; /* no PVDD sense on core */

	p->pd_read = now - last_pd >= 500;
	if (p->pd_read) {
		last_pd = now;
		hifi_i2c_ensure();
		p->sink_ok = hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_STATUS, &p->pd_status) == ESP_OK &&
			     hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_CURRENT, &p->pd_current) == ESP_OK;
		if (!p->sink_ok) p->pd_current = 0;
		bool pd = p->sink_ok && (p->pd_status & 0x18);
		if (pd && !pd_was) {
			t_pd = now;
			cap_n = -1;
		}
		if (!pd) {
			cap_n = -1;
			vreq_sent = 0xFF;
			vreq_v = 0;
		}
		pd_was = pd;
		/* the source's fixed PDOs, once a contract: a charger without 20 V
		 * (5/9/12/15 V phone chargers) is asked for what it has, so the
		 * floor and the governor see the real voltage (review-3 R3-POWER-4) */
		if (pd && cap_n < 0) {
			uint8_t raw[CH224_SRCCAP_LEN];
			int k = 0;
			while (k < CH224_SRCCAP_LEN && hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_SRCCAP + k, &raw[k]) == ESP_OK) k++;
			cap_n = k == CH224_SRCCAP_LEN ? hifi_pd_srccap_fixed(raw, k, cap_v, cap_i, 7) : 0;
			ESP_LOGI(TAG, "source lists %d fixed PDOs%s", cap_n, cap_n ? "" : " (unread: assuming 20 V)");
			for (int j = 0; j < cap_n; j++) ESP_LOGI(TAG, "  %.1f V %.2f A", cap_v[j], cap_i[j]);
		}
		/* once a PD contract is in: 28 V when the source offers EPR, else
		 * the highest fixed PDO up to 20 V */
		uint8_t code = 0;
		float v = pd ? hifi_ch224_request(p->pd_status, cap_v, cap_n > 0 ? cap_n : 0, &code) : 0;
		if (v > 0 && code != vreq_sent && hifi_i2c_write(CH224_I2C_ADDR, CH224_REG_VREQ, code) == ESP_OK) {
			ESP_LOGI(TAG, "asked the charger for %.0f V", v);
			vreq_sent = code;
			vreq_v = v;
			t_pd = now; /* the contract changes: settle from here */
		}
		/* a PD contract in place and settled (the CH224Q's PG also gates
		 * PVDD in hardware): with EPR offered, only once EPR is in, so the
		 * 20 -> 28 V step doesn't land on a closed PVDD switch (R3-POWER-5);
		 * between reads, p keeps the last */
		p->pg = pd && hifi_ch224_settled(p->pd_status, now - t_pd, 3000);
		p->vbus_v = p->sink_ok ? hifi_ch224_vbus(p->pd_status, vreq_v) : 5.0f;
	}
}

void hifi_bsp_power_out(const hifi_power_out_t *o) {
	bool a = o->amp_on, b = a && (zone_b || HIFI_SPLIT);
	/* zone B runs off zone A's clock and syncs only when both leave reset
	 * together (blocks.md): a change of zone B puts both in reset for one
	 * step first */
	if (a && amps_a && b != amps_b && !resync) {
		resync = true;
		a = b = false;
	} else {
		resync = false;
	}
	xSemaphoreTake(exp_lock, portMAX_DELAY);
	uint8_t v = out0;
	if (!a) v = hifi_exp_bit(hifi_exp_bit(v, EXP_AMP_A_RST, false), EXP_AMP_B_RST, false);
	if (v != out0) exp_out(v);
	v = hifi_exp_bit(v, EXP_PVDD_EN, o->pvdd_en);
	if (v != out0) exp_out(v);
	if (a) v = hifi_exp_bit(hifi_exp_bit(v, EXP_AMP_A_RST, true), EXP_AMP_B_RST, b);
	if (v != out0) exp_out(v);
	xSemaphoreGive(exp_lock);
	amps_a = a;
	amps_b = b;
	/* Plus's split pair is one stereo program across two chips */
	hifi_main_gov_amps(HIFI_SPLIT ? 1 : 1 + (a && b));
}

void hifi_bsp_set_zone(bool on) {
	zone_b = HIFI_ZONES > 1 && on;
	config_set_value(NVS_TYPE_STR, "hifi_zone_b", zone_b ? "1" : "0");
}

/* ------------------------------------------------------------------ routing */

/* the sub is digital here, after squeezelite's volume: no boost past 0 dB,
 * which would clip, and down to -40 dB in 0.5 dB steps */
float hifi_bsp_sub_db(float db) {
	if (isnan(db)) db = 0;
	db = roundf(db * 2) / 2;
	return db > 0 ? 0 : db < -40 ? -40 : db;
}

/* DJ direct is the TS5A23157 switch: the line in goes to the amps with no
 * DAC, no volume and no governor (docs/firmware.md) */
bool hifi_bsp_route(hifi_input_t in, bool sub_on, float sub_db, const hifi_vol_t *vol, uint32_t now) {
	sub_gain = sub_on ? powf(10, hifi_bsp_sub_db(sub_db) / 20) : 0;
	return !exp_addr || exp_set(EXP_DJ_DIRECT, in == HIFI_INPUT_DJ_DIRECT) == ESP_OK;
}

/* ------------------------------------------------------------------ the sub DAC */

/*
 * I2S1 as a slave on I2S0's bit and frame clocks, which squeezelite drives
 * on GPIO2/4: the PCM5102A's data costs one pin (blocks.md). It starts
 * once squeezelite's I2S0 has its pins, because setting them up as outputs
 * turns their inputs off; this turns them back on and routes them in.
 */
static void sub_start(void) {
	i2s_config_t c = {
		.mode = I2S_MODE_SLAVE | I2S_MODE_TX,
		.sample_rate = 48000, /* the clocks are I2S0's */
		.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
		.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
		.communication_format = I2S_COMM_FORMAT_STAND_I2S,
		.intr_alloc_flags = ESP_INTR_FLAG_LOWMED, /* level 1 slots may all be taken */
		.dma_buf_count = 4,
		.dma_buf_len = 512,
		.tx_desc_auto_clear = true,
	};
	if (i2s_driver_install(SUB_I2S_NUM, &c, 0, NULL) != ESP_OK) {
		ESP_LOGE(TAG, "sub DAC: no I2S%d", SUB_I2S_NUM);
		return;
	}
	i2s_pin_config_t pins = {.mck_io_num = I2S_PIN_NO_CHANGE, .bck_io_num = I2S_PIN_NO_CHANGE,
				 .ws_io_num = I2S_PIN_NO_CHANGE, .data_out_num = PIN_I2S_DOUT_SUB,
				 .data_in_num = I2S_PIN_NO_CHANGE};
	i2s_set_pin(SUB_I2S_NUM, &pins);
	PIN_INPUT_ENABLE(GPIO_PIN_MUX_REG[PIN_I2S_BCK]);
	PIN_INPUT_ENABLE(GPIO_PIN_MUX_REG[PIN_I2S_LRCK]);
	/* a TX slave takes its clocks on the TX side's inputs (IDF's i2s_set_pin
	 * does the same: s_tx_bck_sig, s_tx_ws_sig) */
	esp_rom_gpio_connect_in_signal(PIN_I2S_BCK, I2S1O_BCK_IN_IDX, false);
	esp_rom_gpio_connect_in_signal(PIN_I2S_LRCK, I2S1O_WS_IN_IDX, false);
	sub_ready = true;
	ESP_LOGI(TAG, "sub DAC on I2S%d, data on GPIO%d", SUB_I2S_NUM, PIN_I2S_DOUT_SUB);
}

/* The sum of the mains to both sub outputs; never waits (this is
 * squeezelite's output thread): what doesn't fit is dropped. */
void hifi_bsp_audio(const uint8_t *buf, uint32_t frames, int bytes_per_frame) {
	static int16_t out[2 * 256];
	if (!sub_ready) return;
	float g = sub_gain * 0.5f;
	for (uint32_t done = 0; done < frames;) {
		uint32_t n = frames - done < 256 ? frames - done : 256;
		for (uint32_t i = 0; i < n; i++) {
			float m;
			if (bytes_per_frame == 8) {
				const int32_t *s = (const int32_t *) buf + 2 * (done + i);
				m = ((float) (s[0] >> 16) + (float) (s[1] >> 16)) * g;
			} else {
				const int16_t *s = (const int16_t *) buf + 2 * (done + i);
				m = ((float) s[0] + (float) s[1]) * g;
			}
			int16_t v = (int16_t) (m > 32767 ? 32767 : m < -32768 ? -32768 : m);
			out[2 * i] = out[2 * i + 1] = v;
		}
		size_t w = 0;
		i2s_write(SUB_I2S_NUM, out, n * 4, &w, 0);
		done += n;
	}
}

/* ------------------------------------------------------------------ controls */

static void IRAM_ATTR exp_isr(void *arg) {
	BaseType_t woke = pdFALSE;
	if (ctrl_task) vTaskNotifyGiveFromISR(ctrl_task, &woke);
	if (woke) portYIELD_FROM_ISR();
}

static bool net_up(void) {
	static const char *ifs[] = {"WIFI_STA_DEF", "ETH_DEF"};
	for (int i = 0; i < 2; i++) {
		esp_netif_ip_info_t ip = {0};
		esp_netif_t *n = esp_netif_get_handle_from_ifkey(ifs[i]);
		if (n && esp_netif_get_ip_info(n, &ip) == ESP_OK && ip.ip.addr) return true;
	}
	return false;
}

/* Plus: knob push play/pause (hold: power), source (hold: mute), next
 * (hold: previous). Ultra's buttons are on its front board. */
static const int PRESS[HIFI_EXP_BUTTONS] = {HIFI_IR_TOGGLE, HIFI_IR_INPUT, HIFI_IR_NEXT, -1};
static const int HOLD[HIFI_EXP_BUTTONS] = {HIFI_IR_POWER, HIFI_IR_MUTE, HIFI_IR_PREV, -1};

static void controls(void *arg) {
	hifi_exp_cfg_t cfg = {.enc_a = EXP_ENC_A, .enc_b = EXP_ENC_B, .edges_per_detent = 4,
#ifdef CONFIG_HIFI_ULTRA
			      .buttons = {-1, -1, -1, -1},
#else
			      .buttons = {EXP_ENC_SW, EXP_BTN_1, EXP_BTN_2, -1},
#endif
	};
	hifi_exp_t e;
	hifi_exp_init(&e, &cfg);
	hifi_exp_event_t ev[4];
	uint32_t tick = 0;
	for (;;) {
		ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10)); /* INT, or 10 ms */
		uint16_t pins;
		if (!exp_read(&pins)) continue;
		in_pins = pins;
		uint32_t now = (uint32_t) (esp_timer_get_time() / 1000);
		int n = hifi_exp_input(&e, pins, now, ev, 4);
		for (int i = 0; i < n; i++) {
			if (ev[i].kind == HIFI_EXP_KNOB) hifi_main_knob(ev[i].steps);
			else if (ev[i].kind == HIFI_EXP_PRESS && PRESS[ev[i].button] >= 0) hifi_main_action(PRESS[ev[i].button]);
			else if (ev[i].kind == HIFI_EXP_LONG && HOLD[ev[i].button] >= 0) hifi_main_action(HOLD[ev[i].button]);
		}
		/* the LED: on, slow blink with no network (setup), fast with the amps
		 * latched off by faults */
		if (++tick % 25 == 0) {
			uint32_t phase = now / (hifi_main_amp_latched() ? 125 : 500);
			bool led = hifi_main_amp_latched() || !net_up() ? phase & 1 : true;
			exp_set(EXP_LED, led);
		}
	}
}

/* squeezelite has put I2S0's bit clock on its pin (the GPIO matrix says) */
static bool i2s0_on_pins(void) {
	uint32_t sel = REG_READ(GPIO_FUNC0_OUT_SEL_CFG_REG + 4 * PIN_I2S_BCK) & GPIO_FUNC0_OUT_SEL_M;
	return sel == I2S0O_BCK_OUT_IDX;
}

static void sub_task(void *arg) {
	/* after squeezelite sets up I2S0's pins, which turns their inputs off */
	while (!i2s0_on_pins()) vTaskDelay(pdMS_TO_TICKS(500));
	vTaskDelay(pdMS_TO_TICKS(500));
	sub_start();
	vTaskDelete(NULL);
}

void hifi_bsp_start(void) {
	if (exp_addr) {
		xTaskCreate(controls, "hifi_ctrl", 3072, NULL, ESP_TASK_PRIO_MIN + 3, &ctrl_task);
		gpio_config_t in = {.pin_bit_mask = BIT64(PIN_EXP_INT), .mode = GPIO_MODE_INPUT, .intr_type = GPIO_INTR_NEGEDGE};
		gpio_config(&in);
		gpio_isr_handler_add(PIN_EXP_INT, exp_isr, NULL); /* squeezelite installed the ISR service */
	}
	xTaskCreatePinnedToCore(sub_task, "hifi_sub", 3072, NULL, ESP_TASK_PRIO_MIN + 1, NULL, 1);
}

/* ------------------------------------------------------------------ info */

void hifi_bsp_info(hifi_info_t *i) {
	i->psram_mb = heap_caps_get_total_size(MALLOC_CAP_SPIRAM) ? 8 : 0; /* WROVER-E N8R8 */
	i->caps = (hifi_caps_t) {
		.dj_direct = true, .eq_min_db = -12, .eq_max_db = 12,
		.sub_count = 2, .sub_crossover = false, .sub_per_sub = false,
		.zones = HIFI_ZONES, .eth = true, .pdo_v_max = 28,
	};
}

void hifi_bsp_snapshot(hifi_snapshot_t *s) {
	s->pvdd_v = s->amp_temp_c = NAN; /* no PVDD or temperature sensing on core */
	s->zones = HIFI_ZONES;
	s->zone_b_on = zone_b;
	s->eth_present = true;
	esp_netif_ip_info_t ip = {0};
	esp_netif_t *eth = esp_netif_get_handle_from_ifkey("ETH_DEF");
	s->eth_up = eth && esp_netif_get_ip_info(eth, &ip) == ESP_OK && ip.ip.addr;
	if (s->eth_up && !s->ip[0]) {
		snprintf(s->ip, sizeof(s->ip), IPSTR, IP2STR(&ip.ip));
		s->net_mode = "sta";
	}
}

void hifi_bsp_selftest(hifi_selftest_t *t) {
	uint8_t v = 0;
	hifi_selftest_add(t, "expander_i2c", exp_addr, exp_addr ? "0x%02x acks" : "none of 0x20, 0x74-0x77 answers", exp_addr);
	bool dac = hifi_i2c_read(ES9018_ADDR, 0x01, &v) == ESP_OK;
	hifi_selftest_add(t, "dac_i2c", dac && !(v & 0xC0), dac ? "0x%02x acks, reg 1 = 0x%02x" : "0x%02x doesn't answer",
			  ES9018_ADDR, v);
	bool adc = hifi_i2c_read(ES7243_ADDR, 0x00, &v) == ESP_OK;
	hifi_selftest_add(t, "adc_i2c", adc, adc ? "0x%02x acks" : "0x%02x doesn't answer", ES7243_ADDR);
	bool otw = !(in_pins >> EXP_AMP_OTW & 1);
	hifi_selftest_add(t, "amp_otw", !otw, otw ? "OTW_CLIP low: too hot or clipping" : "OTW_CLIP high");
}

bool hifi_bsp_parts_ok(void) {
	uint8_t v;
	return exp_addr && hifi_i2c_read(CH224_I2C_ADDR, CH224_REG_STATUS, &v) == ESP_OK &&
	       hifi_i2c_read(ES9018_ADDR, 0x01, &v) == ESP_OK;
}
