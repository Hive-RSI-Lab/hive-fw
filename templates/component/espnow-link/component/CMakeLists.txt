# @NAME@: the link core (@NAME@.c, plain C, host-tested) and its ESP-NOW side
idf_component_register(SRCS "@NAME@.c" "@NAME@_espnow.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_wifi
                       PRIV_REQUIRES esp_timer bt) # bt: esp_bt_controller_get_status, when Bluetooth is on
