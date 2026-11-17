/* @NAME@ on the board: the IDF side (not built by the host test) */
#include "@NAME@.h"
#include "esp_log.h"

static const char *TAG = "@NAME@";

void @NAME@_esp_hello(void) { ESP_LOGI(TAG, "half is %u of 1000", (unsigned) @NAME@_scale(50, 1000)); }
