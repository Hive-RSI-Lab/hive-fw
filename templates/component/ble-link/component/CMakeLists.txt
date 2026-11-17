# @NAME@: the link core (@NAME@.c) and the BLE parts that need no radio
# (@NAME@_bleutil.c), both plain C and host-tested, and its BLE GATT side
idf_component_register(SRCS "@NAME@.c" "@NAME@_bleutil.c" "@NAME@_ble.c"
                       INCLUDE_DIRS "include"
                       REQUIRES bt
                       PRIV_REQUIRES esp_timer)
