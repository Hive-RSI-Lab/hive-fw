/* fw's ADC shim, linked into a QEMU build (fw.json "qemu": {"adc1": ...}).
 *
 * QEMU emulates no SAR ADC: adc1_get_raw() waits for a conversion that never
 * finishes, holding the ADC spinlock, so the core it runs on stops (its
 * RTOS tick too) and the boot hangs wherever the firmware first reads one.
 * fw build --qemu links this file with -Wl,--wrap=adc1_get_raw, and each
 * channel reads the fixed raw value fw.json gives it (FW_ADC1_RAW, eight
 * values for channels 0-7). Built without IDF headers.
 */
#ifndef FW_ADC1_RAW
#define FW_ADC1_RAW 0, 0, 0, 0, 0, 0, 0, 0
#endif

static const int raw[8] = {FW_ADC1_RAW};

int __wrap_adc1_get_raw(int channel)
{
    return channel >= 0 && channel < 8 ? raw[channel] : -1;
}
