/* @NAME@'s bus on an ESP32: the IDF I2C master driver (driver/i2c.h on
 * IDF 4.4 to 5.1, driver/i2c_master.h from 5.2) behind @NAME@_bus_t. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "@NAME@.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
	int port;	 /* I2C_NUM_0 or I2C_NUM_1 */
	int sda, scl;	 /* GPIOs */
	uint32_t hz;	 /* 0: 100 kHz */
	bool install;	 /* install the driver on the port; false when another part of the firmware already did
			    (a codec driver on a shared bus): the bus then only uses it */
	bool pullups;	 /* the internal pull-ups (about 45 kOhm, too weak above 100 kHz; boards have their own) */
} @NAME@_esp_cfg_t;

/* Fills bus for cfg (installing the driver when cfg->install). One bus per
 * port: a second call for the same port returns the same state, and ARG if
 * its cfg differs from the first.
 *
 * With install true the bus recovers itself on a timeout (clocks SDA free,
 * or i2c_master_bus_reset on IDF 5.2 on): nothing else may use the port
 * meanwhile, so give it a single user or serialise its users yourself.
 * With install false it never recovers (the owner's driver does).
 *
 * IDF 5.x: the legacy driver/i2c.h and the new driver/i2c_master.h can't be
 * linked together; the image aborts at boot ("CONFLICT! driver_ng"). This
 * file uses the new driver from IDF 5.2, so install false there needs the
 * port's owner on the new driver too, and IDF 5.4 or later
 * (i2c_master_get_bus_handle); before 5.4 it returns ARG. */
int @NAME@_esp_bus(@NAME@_bus_t *bus, const @NAME@_esp_cfg_t *cfg);

#ifdef __cplusplus
}
#endif
