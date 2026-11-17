/* fw's GPIO input shim, linked into a QEMU build (fw.json "qemu": {"gpio":
 * {"34": 1}}, or any "scenarios").
 *
 * QEMU models no board: an input pin reads whatever its register holds, so a
 * power-good or fault line reads as if the part were absent. fw build --qemu
 * links this file with -Wl,--wrap=gpio_get_level: a pin whose bit is set in
 * fw_gpio_mask reads its bit of fw_gpio_level, the rest read as before.
 * fw.json qemu.gpio gives the levels at boot; a fault step changes them while
 * the firmware runs ({"set": "fw_gpio_level |= 1ULL << 34"}). Built without
 * IDF headers.
 */
#ifndef FW_GPIO_MASK
#define FW_GPIO_MASK 0
#endif
#ifndef FW_GPIO_LEVEL
#define FW_GPIO_LEVEL 0
#endif

volatile unsigned long long fw_gpio_mask = FW_GPIO_MASK;
volatile unsigned long long fw_gpio_level = FW_GPIO_LEVEL;

int __real_gpio_get_level(int gpio_num);

int __wrap_gpio_get_level(int gpio_num)
{
    if (gpio_num >= 0 && gpio_num < 64 && (fw_gpio_mask >> gpio_num & 1)) {
        return (int)(fw_gpio_level >> gpio_num & 1);
    }
    return __real_gpio_get_level(gpio_num);
}
