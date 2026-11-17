/* @NAME@'s bus on an ESP32 (not built by the host test); see include/@NAME@_esp.h
 *
 * IDF 4.4 to 5.1: the legacy driver (driver/i2c.h). Recovery takes the two
 * pins as open-drain GPIO, clocks SDA free and gives them back with
 * i2c_set_pin, so it runs only on a bus this component installed.
 * IDF 5.2 on: driver/i2c_master.h. Its i2c_master_bus_reset clocks the bus
 * free itself, so recovery is only that reset (no line_set/line_get).
 * On a bus another part of the firmware installed (install false), recovery
 * does nothing of its own: the driver's own timeout handling resets it, and
 * taking the lines from under the other user would break its transaction. */
#include "@NAME@_esp.h"

#include <string.h>

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 2, 0)
#define NEW_DRIVER 1
#include "driver/i2c_master.h"
#else
#define NEW_DRIVER 0
#include "driver/i2c.h"
#endif

#define PORTS 2
#define DEVICES 8 /* addresses one bus remembers a device handle for (new driver) */

static const char *TAG = "@NAME@";

typedef struct {
	@NAME@_esp_cfg_t cfg;
	bool open;
#if NEW_DRIVER
	i2c_master_bus_handle_t bus;
	struct {
		uint8_t addr;
		i2c_master_dev_handle_t h;
	} dev[DEVICES];
#endif
} port_t;

static port_t ports[PORTS];

static void wait_us(void *ctx, uint32_t us) {
	(void) ctx;
	esp_rom_delay_us(us);
}

#if NEW_DRIVER

/* i2c_master_probe's answer: NOT_FOUND no ack, TIMEOUT the bus is held */
static int probe_err(esp_err_t e) {
	switch (e) {
	case ESP_OK: return @UNAME@_OK;
	case ESP_ERR_TIMEOUT: return @UNAME@_TIMEOUT;
	case ESP_ERR_NOT_FOUND: return @UNAME@_NACK;
	default: return @UNAME@_BUS;
	}
}

/* A transfer's answer. INVALID_STATE is any failed transfer, a NACK and a
 * held bus alike: a probe of the address tells them apart (no ack: NACK; the
 * bus held: TIMEOUT; acked now: the chip refused a byte, a NACK). TIMEOUT
 * here is the bus lock's wait running out (another user's transfer), not a
 * stuck line: BUS, so the core doesn't recover a bus that isn't stuck. */
static int xfer_err(port_t *p, uint8_t addr, esp_err_t e, uint32_t timeout_ms) {
	switch (e) {
	case ESP_OK: return @UNAME@_OK;
	case ESP_ERR_INVALID_STATE: {
		int r = probe_err(i2c_master_probe(p->bus, addr, (int) timeout_ms));
		return r == @UNAME@_TIMEOUT ? @UNAME@_TIMEOUT : r == @UNAME@_BUS ? @UNAME@_BUS : @UNAME@_NACK;
	}
	default: return @UNAME@_BUS;
	}
}

static i2c_master_dev_handle_t dev(port_t *p, uint8_t addr) {
	int slot = -1;
	for (int i = 0; i < DEVICES; i++) {
		if (p->dev[i].h && p->dev[i].addr == addr) return p->dev[i].h;
		if (!p->dev[i].h && slot < 0) slot = i;
	}
	if (slot < 0) return NULL;
	i2c_device_config_t dc = {
	    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
	    .device_address = addr,
	    .scl_speed_hz = p->cfg.hz,
	};
	if (i2c_master_bus_add_device(p->bus, &dc, &p->dev[slot].h) != ESP_OK) {
		p->dev[slot].h = NULL;
		return NULL;
	}
	p->dev[slot].addr = addr;
	return p->dev[slot].h;
}

static int xfer(void *ctx, uint8_t addr, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, uint32_t timeout_ms) {
	port_t *p = ctx;
	if (!wn && !rn) return probe_err(i2c_master_probe(p->bus, addr, (int) timeout_ms));
	i2c_master_dev_handle_t h = dev(p, addr);
	if (!h) return @UNAME@_BUS;
	esp_err_t e;
	if (!rn) e = i2c_master_transmit(h, w, wn, (int) timeout_ms);
	else if (!wn) e = i2c_master_receive(h, r, rn, (int) timeout_ms);
	else e = i2c_master_transmit_receive(h, w, wn, r, rn, (int) timeout_ms);
	return xfer_err(p, addr, e, timeout_ms);
}

static int reset(void *ctx) {
	port_t *p = ctx;
	return i2c_master_bus_reset(p->bus) == ESP_OK ? @UNAME@_OK : @UNAME@_BUS;
}

static int open_port(port_t *p) {
	if (p->cfg.install) {
		i2c_master_bus_config_t bc = {
		    .i2c_port = p->cfg.port,
		    .sda_io_num = p->cfg.sda,
		    .scl_io_num = p->cfg.scl,
		    .clk_source = I2C_CLK_SRC_DEFAULT,
		    .glitch_ignore_cnt = 7,
		    .flags.enable_internal_pullup = p->cfg.pullups,
		};
		return i2c_new_master_bus(&bc, &p->bus) == ESP_OK ? @UNAME@_OK : @UNAME@_BUS;
	}
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)
	return i2c_master_get_bus_handle(p->cfg.port, &p->bus) == ESP_OK ? @UNAME@_OK : @UNAME@_BUS;
#else
	return @UNAME@_ARG; /* this IDF can't hand over another part's bus handle */
#endif
}

static void fill(@NAME@_bus_t *b, port_t *p) {
	memset(b, 0, sizeof(*b));
	b->xfer = xfer;
	b->wait_us = wait_us;
	if (p->cfg.install) b->reset = reset;
	b->ctx = p;
}

#else /* the legacy driver */

static int err(esp_err_t e) {
	switch (e) {
	case ESP_OK: return @UNAME@_OK;
	case ESP_FAIL: return @UNAME@_NACK; /* i2c_master_cmd_begin: a byte wasn't acked */
	case ESP_ERR_TIMEOUT: return @UNAME@_TIMEOUT;
	default: return @UNAME@_BUS;
	}
}

static int xfer(void *ctx, uint8_t addr, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, uint32_t timeout_ms) {
	port_t *p = ctx;
	i2c_cmd_handle_t c = i2c_cmd_link_create();
	if (!c) return @UNAME@_BUS;
	/* each step can fail (the link's own allocation): a half-built link is never run */
	esp_err_t e = i2c_master_start(c);
	if (e == ESP_OK) e = i2c_master_write_byte(c, (uint8_t) (addr << 1) | I2C_MASTER_WRITE, true);
	if (e == ESP_OK && wn) e = i2c_master_write(c, w, wn, true);
	if (e == ESP_OK && rn) {
		e = i2c_master_start(c);
		if (e == ESP_OK) e = i2c_master_write_byte(c, (uint8_t) (addr << 1) | I2C_MASTER_READ, true);
		if (e == ESP_OK) e = i2c_master_read(c, r, rn, I2C_MASTER_LAST_NACK);
	}
	if (e == ESP_OK) e = i2c_master_stop(c);
	if (e != ESP_OK) {
		i2c_cmd_link_delete(c);
		return @UNAME@_BUS;
	}
	TickType_t t = pdMS_TO_TICKS(timeout_ms);
	e = i2c_master_cmd_begin(p->cfg.port, c, t ? t : 1);
	i2c_cmd_link_delete(c);
	return err(e);
}

static int pin(port_t *p, int line) { return line == @UNAME@_SCL ? p->cfg.scl : p->cfg.sda; }

/* An output mode routes the pin to the GPIO matrix's own output, away from
 * the I2C controller; the input stays connected to both. */
static void line_set(void *ctx, int line, int high) {
	port_t *p = ctx;
	gpio_set_level(pin(p, line), high ? 1 : 0);
	gpio_set_direction(pin(p, line), GPIO_MODE_INPUT_OUTPUT_OD);
}

static int line_get(void *ctx, int line) { return gpio_get_level(pin((port_t *) ctx, line)); }

static int reset(void *ctx) {
	port_t *p = ctx;
	esp_err_t e = i2c_set_pin(p->cfg.port, p->cfg.sda, p->cfg.scl, p->cfg.pullups, p->cfg.pullups, I2C_MODE_MASTER);
	if (e == ESP_OK) e = i2c_reset_tx_fifo(p->cfg.port);
	if (e == ESP_OK) e = i2c_reset_rx_fifo(p->cfg.port);
	return e == ESP_OK ? @UNAME@_OK : @UNAME@_BUS;
}

static int open_port(port_t *p) {
	if (!p->cfg.install) return @UNAME@_OK; /* the owner installed it */
	i2c_config_t c = {
	    .mode = I2C_MODE_MASTER,
	    .sda_io_num = p->cfg.sda,
	    .scl_io_num = p->cfg.scl,
	    .sda_pullup_en = p->cfg.pullups,
	    .scl_pullup_en = p->cfg.pullups,
	    .master.clk_speed = p->cfg.hz,
	};
	if (i2c_param_config(p->cfg.port, &c) != ESP_OK) return @UNAME@_BUS;
	return i2c_driver_install(p->cfg.port, I2C_MODE_MASTER, 0, 0, 0) == ESP_OK ? @UNAME@_OK : @UNAME@_BUS;
}

static void fill(@NAME@_bus_t *b, port_t *p) {
	memset(b, 0, sizeof(*b));
	b->xfer = xfer;
	b->wait_us = wait_us;
	if (p->cfg.install) {
		b->line_set = line_set;
		b->line_get = line_get;
		b->reset = reset;
	}
	b->ctx = p;
}

#endif

/* a later call for an open port must ask for the same bus (hz 0 is the default) */
static bool same(const @NAME@_esp_cfg_t *a, const @NAME@_esp_cfg_t *b) {
	uint32_t bhz = b->hz ? b->hz : 100000;
	return a->sda == b->sda && a->scl == b->scl && a->hz == bhz && a->pullups == b->pullups &&
	       a->install == b->install;
}

int @NAME@_esp_bus(@NAME@_bus_t *bus, const @NAME@_esp_cfg_t *cfg) {
	if (!bus || !cfg || cfg->port < 0 || cfg->port >= PORTS) return @UNAME@_ARG;
	port_t *p = &ports[cfg->port];
	if (!p->open) {
		memset(p, 0, sizeof(*p));
		p->cfg = *cfg;
		if (!p->cfg.hz) p->cfg.hz = 100000;
		int e = open_port(p);
		if (e != @UNAME@_OK) {
			ESP_LOGE(TAG, "port %d (sda %d, scl %d): %s", cfg->port, cfg->sda, cfg->scl, @NAME@_err_name(e));
			return e;
		}
		p->open = true;
	} else if (!same(&p->cfg, cfg)) {
		ESP_LOGE(TAG, "port %d is already open with other pins or settings", cfg->port);
		return @UNAME@_ARG;
	}
	fill(bus, p);
	return @UNAME@_OK;
}
