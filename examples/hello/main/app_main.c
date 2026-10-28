#include <stdio.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "volume.h"

static const char *TAG = "hello";

void app_main(void)
{
    ESP_LOGI(TAG, "hello-fw up: volume 50%% -> attenuation %u", volume_to_attenuation(50));
    for (int i = 0;; i++) {
        ESP_LOGI(TAG, "tick %d", i);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
