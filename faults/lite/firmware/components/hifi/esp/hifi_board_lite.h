/*
 * lite-main's pins and parts, from boards/lite/README.md (the board's own
 * pin map; agreed with its designer). Squeezelite-esp32 owns the codec's
 * I2S, the buttons, IR and the LED through the Kconfig defaults in
 * firmware/patches; this component owns the power path and the amp.
 */
#pragma once

#define HIFI_MODEL "hifi-lite"
#define HIFI_BOARD "lite-main"
#define HIFI_ZONES 1
#define HIFI_FW_VERSION "0.1.0"
#define HIFI_UPSTREAM "squeezelite-esp32 master-v4.3 ee1720c"

#define PIN_PD_PG 4      /* CH224A PG, open drain, low = contract good */
#define PIN_PVDD_EN 18   /* high closes the PVDD switch (ANDed with PG in hardware) */
#define PIN_AMP_SDZ 21   /* TPA3118 SDZ through 100k, high = run */
#define PIN_AMP_FAULTZ 23 /* TPA3118 FAULTZ, open drain, low = fault */
#define ADC_VBUS ADC1_CHANNEL_6 /* GPIO34, VBUS / 11 (100k over 10k) */
#define VBUS_DIVIDER 11.0f

#define I2C_PORT 0       /* shared with squeezelite's codec driver */
#define I2C_SDA 33
#define I2C_SCL 32
#define ES8388_ADDR 0x10

/* ES8388 registers this component touches (datasheet rev 5.0) */
#define ES8388_ADCPOWER 0x03 /* bit7/6 power down the analog inputs */
#define ES8388_DACPOWER 0x04 /* bits 5..2: LOUT1 ROUT1 LOUT2 ROUT2 on */
#define ES8388_DACCONTROL17 0x27 /* left mixer: bit7 DAC, bit6 LIN, bits5:3 LIN gain */
#define ES8388_DACCONTROL20 0x2A /* right mixer, same layout */
#define ES8388_LOUT1VOL 0x2E /* speaker amp L: same scale as LOUT2 */
#define ES8388_ROUT1VOL 0x2F
/* the speaker outputs' highest code: 27 = -4.5 dB. The first-spin board review
 * (task 261029-020622, docs/review-2/firmware-requirements.md) caps LOUT1/ROUT1
 * at -4.5 dB and the line-in bypass at -3 dB: 0 dB overdrives the amp */
#define ES8388_OUT1_FULL 27
#define ES8388_MIX_LIN_M3DB 0x18 /* DACCONTROL17/20 bits 5:3 = 011: LIN at -3 dB */
#define ES8388_LOUT2VOL 0x30 /* sub out L: 0 = -45 dB, 30 = 0 dB, 33 = +4.5 dB */
#define ES8388_ROUT2VOL 0x31

#define TONE_RATE 48000

/* what this board's firmware does besides the shared parts */
#define HIFI_HAS_CAPTURE 1 /* the ES8388's ADC: line in and the loopback taps */

/* ES8388 ADC, for the captures (docs/remote.md). Values from the datasheet,
 * not yet checked on a board. */
#define ES8388_ADCCONTROL1 0x09  /* PGA gain, 0 = 0 dB */
#define ES8388_ADCCONTROL2 0x0A  /* bits 7:6 LINSEL, 5:4 RINSEL: 00 = input 1, 01 = input 2 */
#define ES8388_ADCCONTROL4 0x0C  /* I2S, 16 bit */
#define ES8388_ADCCONTROL5 0x0D  /* MCLK/LRCK 256 */
#define ES8388_ADCCONTROL8 0x10  /* left ADC digital volume, 0 = 0 dB */
#define ES8388_ADCCONTROL9 0x11
#define ES8388_ADCCONTROL10 0x12 /* bits 7:6 ALC: 00 = off */
#define ES8388_ADCPOWER_CAPTURE 0x08 /* inputs and ADCs on, mic bias off */

/* The loopback taps: each speaker's + leg through 100k over 10k into LIN2/RIN2.
 * lite-main rev A has no taps (they come with the board rev the review task
 * 261029-020622 builds); set HIFI_HAS_TAPS for that rev or "hifi_taps"=1 in NVS. */
#ifndef HIFI_HAS_TAPS
#define HIFI_HAS_TAPS 0
#endif
#define TAP_DIVIDER 11.0f      /* 100k + 10k over 10k */
#define ADC_FS_PEAK_V 1.27f    /* ES8388 ADC full scale at 0 dB PGA, about 0.9 Vrms: calibrate at bring-up */
#define LINE_DIVIDER 2.0f      /* the line in's -6 dB attenuator (R30/R31) */
