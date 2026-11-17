/*
 * A bus on the build machine for @NAME@'s host test and fuzz harness (not
 * built for the board: CMakeLists.txt leaves mock/ out). Chips with 256
 * registers and an auto-incrementing pointer, the faults a real bus has
 * (NACK, a timeout, a bus error, SDA held by a chip for k clocks or for
 * good, SCL held), and the lines as recovery sees them.
 */
#pragma once

#include <stdint.h>

#include "@NAME@.h"

#define @UNAME@_MOCK_CHIPS 4
#define @UNAME@_MOCK_FOREVER 255 /* sda_stuck: a chip that never lets SDA go */

typedef struct {
	uint8_t addr; /* 0: no chip in this slot */
	uint8_t reg[256];
	uint8_t ptr;
	uint32_t writes; /* data bytes written to reg[] */
} @NAME@_mock_chip_t;

typedef struct {
	@NAME@_mock_chip_t chip[@UNAME@_MOCK_CHIPS];
	/* faults, each for the next n transactions */
	unsigned nack, timeout, buserr;
	unsigned sda_stuck; /* SDA held low until this many SCL clocks, or forever; transactions time out meanwhile */
	int scl_stuck;	    /* SCL held low: transactions time out and recovery can't clock */
	unsigned sda_again; /* once SDA is free, the next SCL fall makes the chip hold it for this many more clocks */
	/* what happened */
	unsigned xfers, clocks, stops, resets;
	uint32_t waited_us;
	int scl, sda; /* what the controller drives (1 = let go) */
} @NAME@_mock_t;

/* m empty (no chips, no faults, lines high) and b a bus over it */
void @NAME@_mock_init(@NAME@_mock_t *m, @NAME@_bus_t *b);
/* a chip at addr; its registers, or NULL when all slots are taken */
@NAME@_mock_chip_t *@NAME@_mock_add(@NAME@_mock_t *m, uint8_t addr);
/* the lines as a chip sees them: the controller's level ANDed with the faults */
int @NAME@_mock_line(const @NAME@_mock_t *m, int line);
