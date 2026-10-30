/*
 * The core board's pins and parts (Plus and Ultra), from
 * boards/core/architecture/blocks.md: its ESP32 GPIO map, the I/O expander
 * map and the FFC to Ultra's front board.
 *
 * Squeezelite-esp32 owns I2S0 (the Sabre DAC's data, the line-in ADC's),
 * IR and Ethernet through the Kconfig defaults in firmware/patches. This
 * component owns the I2C bus's other parts: the PD sink, the expander (amp
 * resets and faults, PVDD, the DAC's reset, the PHY's clock, DJ direct, the
 * LED, the knob and buttons) and the current sense, and drives the sub DAC
 * from I2S1.
 */
#pragma once

#include "sdkconfig.h"

#ifdef CONFIG_HIFI_ULTRA
#define HIFI_MODEL "hifi-ultra"
#define HIFI_ZONES 2
#define HIFI_SPLIT 0
#else
#define HIFI_MODEL "hifi-plus"
#define HIFI_ZONES 1
/* Plus fits zone B too and splits the pair: L plays on zone A (J6), R on
 * zone B (J11), one channel per TPA3220, so each chip carries half the heat
 * (docs/review-2/heatsink-options.md). Both amps run whenever the amp is on */
#define HIFI_SPLIT 1
#endif
#define HIFI_BOARD "core"
#define HIFI_FW_VERSION "0.1.0"
#define HIFI_UPSTREAM "squeezelite-esp32 master-v4.3 ee1720c"

#define I2C_PORT 1 /* squeezelite's system I2C (I2C_CONFIG): the DAC has no driver of its own */
#define I2C_SDA 13
#define I2C_SCL 14

/* I2S0 (squeezelite's DAC_CONFIG); I2S1 runs as a slave on the same clocks
 * and sends the sub DAC its data, so the PCM5102A costs one pin */
#define PIN_I2S_BCK 2
#define PIN_I2S_LRCK 4
#define PIN_I2S_DOUT_SUB 12 /* MTDI strap: the PCM5102A's DIN is high-Z at boot */
#define SUB_I2S_NUM 1

#define PIN_EXP_INT 39 /* the expander's INT, open drain, low = an input changed */
#define ADC_ISENSE ADC1_CHANNEL_6 /* GPIO34: INA186A1, 25 V/V over 10 mOhm */
#define ISENSE_V_PER_A 0.25f
#define ISENSE_RAW_FLOOR 8 /* ADC counts: under this the ADC is in its dead zone */

/* RMII and SMI: fixed by the ESP32's EMAC (and ETH_CONFIG in the patch);
 * here so the pin check reads them against the board too */
#define PIN_ETH_CLK 0    /* the 50 MHz oscillator in; it runs only while PHY_RST is high */
#define PIN_RMII_TXD0 19
#define PIN_RMII_TX_EN 21
#define PIN_RMII_TXD1 22
#define PIN_RMII_RXD0 25
#define PIN_RMII_RXD1 26
#define PIN_RMII_CRS_DV 27
#define PIN_MDC 23
#define PIN_MDIO 18

/* the I2C parts */
#define ES9018_ADDR 0x48 /* Sabre ES9018K2M, the mains DAC */
#define ES7243_ADDR 0x10 /* ES7243E, the line-in ADC */
/* TCA9555 at 0x20 on the board as drawn; the first-spin review swaps in a
 * TCA9539 (same registers, a reset pin), whose address is 0x74-0x77.
 * The firmware takes the first that answers. */
#define EXP_ADDRS {0x20, 0x74, 0x75, 0x76, 0x77}
#define EXP_REG_IN0 0x00
#define EXP_REG_OUT0 0x02
#define EXP_REG_CFG0 0x06 /* 1 = input */

/* expander port 0: outputs, each with a 100k pull-down (off at reset) */
#define EXP_AMP_A_RST 0   /* high runs zone A's TPA3221 */
#define EXP_AMP_B_RST 1   /* zone B's (Ultra); release both in one write to sync them */
#define EXP_DJ_DIRECT 2   /* high: the line in goes straight to the amps */
#define EXP_PVDD_EN 3
#define EXP_DAC_EN 4      /* ES9018K2M RESETB and PCM5102A XSMT */
#define EXP_PHY_RST 5     /* the Ethernet clock's OE, and the LAN8720's nRST after it */
#define EXP_LCD_BL 6      /* Ultra's front board backlight */
#define EXP_LED 7         /* the red status LED */
/* port 1: inputs, pulled up (bit n is P1(n-8) in hifi_exp's 16 bits) */
#define EXP_AMP_FAULT 8   /* both amps' FAULT, wired-OR, low = fault */
#define EXP_AMP_OTW 9     /* OTW_CLIP, wired-OR, low = too hot or clipping */
#define EXP_ENC_A 10
#define EXP_ENC_B 11
#define EXP_ENC_SW 12     /* Plus: the knob's push */
#define EXP_BTN_1 13      /* Plus: source */
#define EXP_BTN_2 14      /* Plus: next */
#define EXP_TAP_SEL 15    /* P17 (TAP_SEL_IO): the ADC's tap/line-in select through Q7 (review-2
                          * AUDIO-3, DIGITAL-16); left an input, Q7's base pull-down keeps
                          * the ADC on the RCA line in */
#define EXP_CFG0 0x00
#define EXP_CFG1 0xFF

#define TONE_RATE 48000

/* what this board's firmware does besides the shared parts */
#define HIFI_HAS_CAPTURE 0 /* the ES7243E's registers aren't set up yet */
