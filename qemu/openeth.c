/* fw's open_eth shim, linked into a QEMU build (fw.json "qemu": {"eth": "emac"}).
 *
 * QEMU emulates no ESP32 EMAC, only an OpenCores Ethernet MAC with a
 * DP83848-like PHY. Firmware that brings up Ethernet through the EMAC (a
 * LAN8720, IP101, RTL8201 or KSZ80xx board, or squeezelite-esp32 told it has
 * one) gets IDF's open_eth MAC and the DP83848 driver instead: fw build --qemu
 * links this file with -Wl,--wrap for each constructor below, so the
 * firmware's own code runs unchanged. Built without IDF headers, so it only
 * depends on the constructors' ABI: pointers in, a pointer out.
 */
struct esp_eth_mac_s;
struct esp_eth_phy_s;

struct esp_eth_mac_s *esp_eth_mac_new_openeth(const void *mac_config);
struct esp_eth_phy_s *esp_eth_phy_new_dp83848(const void *phy_config);

#if FW_IDF_MAJOR >= 5
/* IDF 5: esp_eth_mac_new_esp32(esp32_emac_config, mac_config) */
struct esp_eth_mac_s *__wrap_esp_eth_mac_new_esp32(const void *emac_config, const void *mac_config)
{
    (void)emac_config;
    return esp_eth_mac_new_openeth(mac_config);
}
#else
/* IDF 4: esp_eth_mac_new_esp32(mac_config) */
struct esp_eth_mac_s *__wrap_esp_eth_mac_new_esp32(const void *mac_config)
{
    return esp_eth_mac_new_openeth(mac_config);
}
#endif

#define FW_PHY(name) \
    struct esp_eth_phy_s *__wrap_esp_eth_phy_new_##name(const void *c) { return esp_eth_phy_new_dp83848(c); }
FW_PHY(lan87xx)
FW_PHY(ip101)
FW_PHY(rtl8201)
FW_PHY(ksz8041)
FW_PHY(ksz8081)
FW_PHY(ksz80xx)
