#include "rr_api.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rr_b64.h"
#include "rr_json.h"
#include "rr_ws_proto.h"

#define PREFIX "/api/v1/"

static int error(char *out, size_t cap, int status, const char *msg) {
	hj_t j;
	hj_init(&j, out, cap);
	hj_obj(&j, NULL);
	hj_str(&j, "error", msg);
	hj_obj_end(&j);
	return status;
}


void rr_selftest_add(rr_selftest_t *t, const char *name, bool pass, const char *fmt, ...) {
	if (t->n >= RR_SELFTEST_MAX) return;
	rr_check_t *c = &t->c[t->n++];
	snprintf(c->name, sizeof(c->name), "%s", name);
	c->pass = pass;
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(c->value, sizeof(c->value), fmt, ap);
	va_end(ap);
}


/* ------------------------------------------------------------------ routes */

static int r_info(const rr_ops_t *ops, char *out, size_t cap) {
	rr_info_t i = {0};
	ops->info(&i);
	hj_t j;
	hj_init(&j, out, cap);
	hj_obj(&j, NULL);
	hj_str(&j, "model", i.model);
	hj_str(&j, "board", i.board);
	hj_str(&j, "fw", i.fw);
	hj_int(&j, "api", RR_API_VERSION);
	hj_str(&j, "mac", i.mac);
	hj_int(&j, "flash_mb", i.flash_mb);
	hj_obj_end(&j);
	return 200;
}

static int r_reboot(const rr_ops_t *ops, char *out, size_t cap) {
	ops->reboot();
	snprintf(out, cap, "{\"ok\":true,\"reboot\":true}");
	return 200;
}

static int r_selftest(const rr_ops_t *ops, char *out, size_t cap) {
	rr_selftest_t *t = calloc(1, sizeof(*t));
	if (!t) return error(out, cap, 500, "out of memory");
	ops->selftest(t);
	bool pass = t->n > 0;
	for (int i = 0; i < t->n; i++) pass &= t->c[i].pass;
	hj_t j;
	hj_init(&j, out, cap);
	hj_obj(&j, NULL);
	hj_bool(&j, "pass", pass);
	hj_arr(&j, "checks");
	for (int i = 0; i < t->n; i++) {
		hj_obj(&j, NULL);
		hj_str(&j, "name", t->c[i].name);
		hj_bool(&j, "pass", t->c[i].pass);
		hj_str(&j, "value", t->c[i].value);
		hj_obj_end(&j);
	}
	hj_arr_end(&j);
	hj_obj_end(&j);
	free(t);
	return 200;
}


static int r_ota_get(const rr_ops_t *ops, char *out, size_t cap) {
	if (!ops->ota || !ops->ota_status) return error(out, cap, 404, "no OTA in this build");
	rr_ota_status_t st = {0};
	ops->ota_status(&st);
	const rr_ota_t *o = ops->ota;
	rr_info_t i = {0};
	ops->info(&i);
	hj_t j;
	hj_init(&j, out, cap);
	hj_obj(&j, NULL);
	hj_str(&j, "slot", st.slot);
	hj_str(&j, "fw", i.fw);
	hj_str(&j, "state", st.state ? st.state : "unknown");
	if (o->active || o->done) {
		hj_obj(&j, "update");
		hj_bool(&j, "active", o->active);
		hj_int(&j, "size", (long long) o->size);
		hj_int(&j, "written", (long long) o->written);
		hj_str(&j, "version", o->version);
		hj_str(&j, "slot", o->slot);
		hj_obj_end(&j);
	} else {
		hj_null(&j, "update");
	}
	if (st.last_from[0] || st.last_reason[0]) {
		hj_obj(&j, "last_rollback");
		hj_str(&j, "from", st.last_from);
		hj_str(&j, "reason", st.last_reason);
		hj_obj_end(&j);
	} else {
		hj_null(&j, "last_rollback");
	}
	hj_obj_end(&j);
	return 200;
}

static int ok_reply(char *out, size_t cap) {
	snprintf(out, cap, "{\"ok\":true}");
	return 200;
}

/* one step of an update over the API, under the session's lock (r_ota) */
static int r_ota_step(const rr_ops_t *ops, const char *what, const char *body, char *out, size_t cap,
		      bool *restart) {
	rr_ota_t *o = ops->ota;
	/* a task is running a whole update (rr_ota_claim) */
	if (o->held) return error(out, cap, 409, "an update is already running");
	const char *err = "update failed";
	int st;
	hj_t j;
	if (!strcmp(what, "begin")) {
		double size;
		char sha[72] = "", version[32] = "";
		if (!hj_get_num(body, "size", &size) || !(size > 0)) return error(out, cap, 400, "size is required");
		/* range-checked before the cast: past size_t it is undefined (32 bits on the board) */
		if (!(size <= 4294967295.0)) return error(out, cap, 400, "image is larger than the app slot");
		hj_get_str(body, "sha256", sha, sizeof(sha));
		hj_get_str(body, "version", version, sizeof(version));
		if ((st = rr_ota_begin(o, (size_t) size, sha, version, &err)) != 200)
			return error(out, cap, st, err);
		hj_init(&j, out, cap);
		hj_obj(&j, NULL);
		hj_bool(&j, "ok", true);
		hj_str(&j, "slot", o->slot);
		hj_int(&j, "chunk_max", RR_OTA_CHUNK_MAX);
		hj_obj_end(&j);
		return 200;
	}
	if (!strcmp(what, "chunk")) {
		double off;
		const char *data;
		size_t dlen;
		/* range-checked before the cast, as size is */
		if (!hj_get_num(body, "off", &off) || !(off >= 0 && off <= 4294967295.0))
			return error(out, cap, 400, "off is required");
		if (!hj_get_str_span(body, "data", &data, &dlen)) return error(out, cap, 400, "data (base64) is required");
		uint8_t *buf = malloc(RR_OTA_CHUNK_MAX);
		if (!buf) return error(out, cap, 500, "out of memory");
		long n = dlen <= rr_b64_len(RR_OTA_CHUNK_MAX) ? rr_b64_decode(data, dlen, buf, RR_OTA_CHUNK_MAX) : -1;
		st = n < 0 ? (err = "data isn't base64 of at most 4096 bytes", 400)
			   : rr_ota_chunk(o, (size_t) off, buf, (size_t) n, &err);
		free(buf);
		hj_init(&j, out, cap);
		hj_obj(&j, NULL);
		if (st == 200) hj_bool(&j, "ok", true);
		else hj_str(&j, "error", err);
		hj_int(&j, "next", (long long) o->written); /* where the sender goes on from */
		hj_obj_end(&j);
		return st;
	}
	if (!strcmp(what, "finish")) {
		bool reboot = true;
		hj_get_bool(body, "reboot", &reboot);
		if ((st = rr_ota_finish(o, &err)) != 200) return error(out, cap, st, err);
		*restart = reboot;
		hj_init(&j, out, cap);
		hj_obj(&j, NULL);
		hj_bool(&j, "ok", true);
		hj_bool(&j, "reboot", reboot);
		hj_str(&j, "slot", o->slot);
		hj_obj_end(&j);
		return 200;
	}
	if (!strcmp(what, "abort")) {
		rr_ota_abort(o);
		return ok_reply(out, cap);
	}
	return error(out, cap, 404, "no such path");
}

static int r_ota(const rr_ops_t *ops, const char *what, const char *body, char *out, size_t cap) {
	rr_ota_t *o = ops->ota;
	if (!o) return error(out, cap, 404, "no OTA in this build");
	bool restart = false;
	rr_ota_lock(o);
	int st = r_ota_step(ops, what, body, out, cap, &restart);
	rr_ota_unlock(o);
	if (restart) ops->reboot(); /* after the reply has gone */
	return st;
}


static int r_wifi(const rr_ops_t *ops, const char *body, char *out, size_t cap) {
	if (!ops->set_wifi) return error(out, cap, 404, "no Wi-Fi setup in this build");
	char ssid[33], pw[65] = "";
	if (!hj_get_str(body, "ssid", ssid, sizeof(ssid)) || !ssid[0]) return error(out, cap, 400, "ssid is required");
	hj_get_str(body, "password", pw, sizeof(pw));
	if (ops->set_wifi(ssid, pw)) return error(out, cap, 500, "couldn't start joining");
	return ok_reply(out, cap);
}

static int r_remote(const rr_ops_t *ops, bool post, const char *body, char *out, size_t cap) {
	if (!ops->remote_status) return error(out, cap, 404, "no relay in this build");
	if (post) {
		char url[128] = "", user[48] = "", pw[96] = "", prefix[24] = "rr", key[66] = "";
		if (!ops->remote_set) return error(out, cap, 404, "no relay in this build");
		const char *sv;
		size_t sl;
		/* a cut-off url or token would dial somewhere else, or fail without saying why */
		if ((hj_get_str_span(body, "url", &sv, &sl) && sl >= sizeof(url)) ||
		    (hj_get_str_span(body, "user", &sv, &sl) && sl >= sizeof(user)) ||
		    (hj_get_str_span(body, "password", &sv, &sl) && sl >= sizeof(pw)))
			return error(out, cap, 400, "url, user or password is too long");
		hj_get_str(body, "url", url, sizeof(url));
		hj_get_str(body, "key", key, sizeof(key));
		bool ws = url[0] != '\0';
		char host[RR_WS_HOST_MAX], path[RR_WS_PATH_MAX];
		int port;
		if (ws && rr_ws_url_parse(url, host, &port, path))
			return error(out, cap, 400, "url must be wss://host/path (the Hive's endpoint), or empty to turn it off");
		if (ws && !rr_ws_key_ok(key)) return error(out, cap, 400, "a wss:// link needs its key: 64 hex digits");
		if (!ws) key[0] = '\0';
		hj_get_str(body, "user", user, sizeof(user));
		hj_get_str(body, "password", pw, sizeof(pw));
		hj_get_str(body, "prefix", prefix, sizeof(prefix));
		if (!prefix[0] || strpbrk(prefix, "+#/ ")) return error(out, cap, 400, "prefix must be one topic level");
		/* the service token goes into the dial's headers: nothing that could start another one */
		char hd[RR_WS_HEADERS_MAX];
		if (ws && rr_ws_headers(hd, sizeof(hd), user, pw, "000000000000", key) < 0)
			return error(out, cap, 400, "user and password must be printable, with a password when there is a user");
		if (ops->remote_set(url, user, pw, prefix, key)) return error(out, cap, 500, "couldn't save the relay settings");
	}
	rr_remote_status_t r = {0};
	ops->remote_status(&r);
	hj_t j;
	hj_init(&j, out, cap);
	hj_obj(&j, NULL);
	hj_bool(&j, "configured", r.configured);
	hj_bool(&j, "connected", r.connected);
	hj_str(&j, "url", r.url[0] ? r.url : NULL);
	hj_str(&j, "user", r.user[0] ? r.user : NULL);
	hj_str(&j, "prefix", r.prefix);
	hj_str(&j, "dev", r.dev);
	hj_bool(&j, "has_key", r.has_key);
	hj_int(&j, "keys", r.keys);
	hj_int(&j, "accepted", r.accepted);
	hj_int(&j, "rejected", r.rejected);
	hj_obj_end(&j);
	return 200;
}


size_t rr_api_reply_cap(const char *path, const char *body) {
	(void) path;
	(void) body;
	return 4096;
}

int rr_api_handle(const rr_ops_t *ops, const char *method, const char *path,
		  const char *body, char *out, size_t cap) {
	char route[32];
	size_t plen = strlen(PREFIX);
	if (!path || strncmp(path, PREFIX, plen)) return error(out, cap, 404, "no such path");
	snprintf(route, sizeof(route), "%s", path + plen);
	route[strcspn(route, "?")] = '\0';
	bool get = !strcmp(method, "GET"), post = !strcmp(method, "POST");

	if (post && (!body || !*body)) body = "{}";
	if (post && !hj_valid_object(body)) return error(out, cap, 400, "body must be a JSON object");

	int status;
	if (!strcmp(route, "info")) status = get ? r_info(ops, out, cap) : -1;
	else if (!strcmp(route, "selftest")) status = post ? r_selftest(ops, out, cap) : -1;
	else if (!strcmp(route, "reboot")) status = post ? r_reboot(ops, out, cap) : -1;
	else if (!strcmp(route, "ota")) status = get ? r_ota_get(ops, out, cap) : -1;
	else if (!strncmp(route, "ota/", 4)) status = post ? r_ota(ops, route + 4, body, out, cap) : -1;
	else if (!strcmp(route, "wifi")) status = post ? r_wifi(ops, body, out, cap) : -1;
	else if (!strcmp(route, "remote")) status = get || post ? r_remote(ops, post, body, out, cap) : -1;
	else return error(out, cap, 404, "no such path");

	if (status < 0) return error(out, cap, 405, "wrong method for this path");
	return status;
}
