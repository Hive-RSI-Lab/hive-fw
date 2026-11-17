# @NAME@: the driver (@NAME@.c, plain C, host-tested against mock/) and its IDF bus.
# mock/ is the host test's bus and is not built for the board.
idf_component_register(SRCS "@NAME@.c" "@NAME@_esp.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver
                       PRIV_REQUIRES esp_rom log)
