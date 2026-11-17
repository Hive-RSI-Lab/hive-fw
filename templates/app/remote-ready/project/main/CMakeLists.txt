idf_component_register(SRCS "app_main.c" INCLUDE_DIRS "."
		       PRIV_REQUIRES rr nvs_flash esp_wifi esp_netif esp_event app_update console spi_flash)
