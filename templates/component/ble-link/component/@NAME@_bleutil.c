/* @NAME@ over BLE, the parts that need no radio (see @NAME@_bleutil.h) */
#include "@NAME@_bleutil.h"

#include <string.h>

#define AD_FLAGS 0x01
#define AD_MFR 0xff
#define FLAG_GEN_DISC 0x02
#define FLAG_BREDR_NOT_SPT 0x04

static void mark(const uint8_t mac[6], uint8_t *m) {
	m[0] = 0xff, m[1] = 0xff, m[2] = 'h', m[3] = 'l';
	memcpy(m + 4, mac, 6);
}

size_t @NAME@_adv_build(const uint8_t mac[6], uint8_t *out, size_t cap) {
	if (!mac || !out || cap < @UNAME@_ADV_LEN) return 0;
	uint8_t *p = out;
	*p++ = 2, *p++ = AD_FLAGS, *p++ = FLAG_GEN_DISC | FLAG_BREDR_NOT_SPT;
	*p++ = 1 + @UNAME@_MARK_LEN, *p++ = AD_MFR;
	mark(mac, p);
	return @UNAME@_ADV_LEN;
}

bool @NAME@_adv_has_mark(const uint8_t *adv, size_t n, const uint8_t mac[6]) {
	if (!adv || !mac) return false;
	uint8_t want[@UNAME@_MARK_LEN];
	mark(mac, want);
	for (size_t i = 0; i < n;) {
		size_t l = adv[i];
		if (!l) break; /* the rest is padding */
		if (i + 1 + l > n) return false;
		if (adv[i + 1] == AD_MFR && l - 1 == @UNAME@_MARK_LEN && !memcmp(adv + i + 2, want, @UNAME@_MARK_LEN))
			return true;
		i += 1 + l;
	}
	return false;
}

static uint64_t u64(const uint8_t *m) {
	uint64_t v = 0;
	for (int i = 0; i < 6; i++) v = v << 8 | m[i];
	return v;
}

void @NAME@_peer_ble_addr(const uint8_t peer_wifi[6], const uint8_t self_wifi[6], const uint8_t self_bt[6],
			  uint8_t out[6]) {
	if (!memcmp(self_bt, self_wifi, 5)) {
		/* IDF's rule: the offset is added to the last byte only, no carry */
		memcpy(out, peer_wifi, 6);
		out[5] = (uint8_t) (peer_wifi[5] + self_bt[5] - self_wifi[5]);
		return;
	}
	uint64_t v = (u64(peer_wifi) + u64(self_bt) - u64(self_wifi)) & 0xffffffffffffULL;
	for (int i = 5; i >= 0; i--, v >>= 8) out[i] = (uint8_t) v;
}

uint16_t @NAME@_interval_units(float ms) {
	if (!(ms >= 7.5f && ms <= 100.0f)) return 0; /* NaN too */
	return (uint16_t) (ms / 1.25f + 0.5f);
}

uint16_t @NAME@_payload_for_mtu(uint16_t mtu) {
	if (mtu < 23) return 0;
	return mtu - 3 > @UNAME@_BLE_PAYLOAD ? @UNAME@_BLE_PAYLOAD : mtu - 3;
}
