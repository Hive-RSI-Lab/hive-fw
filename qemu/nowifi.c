/* fw's Wi-Fi shim, linked into a QEMU build (fw.json "qemu": {"wifi": "off"}).
 *
 * QEMU emulates no radio: esp_wifi_start() calibrates the PHY through RF
 * registers QEMU doesn't map, and the chip panics (LoadStorePIFAddrError in
 * register_chipv7_phy). fw build --qemu links this file with -Wl,--wrap for
 * each call below, which then succeed without touching the radio: the
 * firmware sees a Wi-Fi that started and finds no network (no scan results,
 * no connection), and carries on, over open_eth when it has that.
 * Built without IDF headers: each returns ESP_OK (0).
 */
#define FW_OK(name) \
    int __wrap_##name(void) { return 0; }
FW_OK(esp_wifi_start)
FW_OK(esp_wifi_stop)
FW_OK(esp_wifi_connect)
FW_OK(esp_wifi_disconnect)
FW_OK(esp_wifi_scan_start)
FW_OK(esp_wifi_scan_stop)
