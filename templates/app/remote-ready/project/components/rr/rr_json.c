#include "rr_json.h"

#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ writer */

void hj_init(hj_t *j, char *buf, size_t cap) {
	memset(j, 0, sizeof(*j));
	j->buf = buf;
	j->cap = cap;
	if (cap) buf[0] = '\0';
}

static void put(hj_t *j, const char *s, size_t n) {
	if (j->overflow) return;
	if (j->len + n + 1 > j->cap) {
		j->overflow = true;
		return;
	}
	memcpy(j->buf + j->len, s, n);
	j->len += n;
	j->buf[j->len] = '\0';
}

static void puts_(hj_t *j, const char *s) { put(j, s, strlen(s)); }

static void put_escaped(hj_t *j, const char *s) {
	put(j, "\"", 1);
	for (; *s; s++) {
		unsigned char c = (unsigned char) *s;
		char esc[8];
		if (c == '"' || c == '\\') {
			esc[0] = '\\';
			esc[1] = (char) c;
			put(j, esc, 2);
		} else if (c < 0x20) {
			snprintf(esc, sizeof(esc), "\\u%04x", c);
			puts_(j, esc);
		} else {
			put(j, (const char *) &c, 1);
		}
	}
	put(j, "\"", 1);
}

static void key(hj_t *j, const char *k) {
	if (j->depth > 0) {
		if (!j->first[j->depth]) put(j, ",", 1);
		j->first[j->depth] = false;
	}
	if (k) {
		put_escaped(j, k);
		put(j, ":", 1);
	}
}

static void open_(hj_t *j, const char *k, const char *brace) {
	key(j, k);
	puts_(j, brace);
	if (j->depth + 1 < HJ_MAX_DEPTH) j->depth++;
	else j->overflow = true;
	j->first[j->depth] = true;
}

static void close_(hj_t *j, const char *brace) {
	if (j->depth > 0) j->depth--;
	puts_(j, brace);
}

void hj_obj(hj_t *j, const char *k) { open_(j, k, "{"); }
void hj_obj_end(hj_t *j) { close_(j, "}"); }
void hj_arr(hj_t *j, const char *k) { open_(j, k, "["); }
void hj_arr_end(hj_t *j) { close_(j, "]"); }

void hj_str(hj_t *j, const char *k, const char *v) {
	key(j, k);
	if (v) put_escaped(j, v);
	else puts_(j, "null");
}

void hj_num(hj_t *j, const char *k, double v, int decimals) {
	char tmp[40];
	key(j, k);
	if (isnan(v) || isinf(v)) {
		puts_(j, "null");
		return;
	}
	snprintf(tmp, sizeof(tmp), "%.*f", decimals, v);
	/* "-0.0" reads oddly in a log */
	if (tmp[0] == '-' && strspn(tmp + 1, "0.") == strlen(tmp + 1)) memmove(tmp, tmp + 1, strlen(tmp));
	puts_(j, tmp);
}

void hj_int(hj_t *j, const char *k, long long v) {
	char tmp[24];
	key(j, k);
	snprintf(tmp, sizeof(tmp), "%lld", v);
	puts_(j, tmp);
}

void hj_bool(hj_t *j, const char *k, bool v) {
	key(j, k);
	puts_(j, v ? "true" : "false");
}

void hj_null(hj_t *j, const char *k) {
	key(j, k);
	puts_(j, "null");
}

/* ------------------------------------------------------------------ reader */

static const char *ws(const char *p) {
	while (*p && isspace((unsigned char) *p)) p++;
	return p;
}

/* Skips a string starting at '"'; returns the char after the closing quote. */
static const char *skip_str(const char *p) {
	for (p++; *p; p++) {
		if (*p == '\\') {
			if (!*++p) return NULL;
		} else if (*p == '"') {
			return p + 1;
		}
	}
	return NULL;
}

static const char *skip_value(const char *p);

static const char *skip_container(const char *p, char open, char close) {
	p = ws(p + 1);
	if (*p == close) return p + 1;
	for (;;) {
		if (open == '{') {
			if (*p != '"' || !(p = skip_str(p))) return NULL;
			p = ws(p);
			if (*p++ != ':') return NULL;
		}
		if (!(p = skip_value(ws(p)))) return NULL;
		p = ws(p);
		if (*p == ',') {
			p = ws(p + 1);
			continue;
		}
		if (*p == close) return p + 1;
		return NULL;
	}
}

static const char *skip_value(const char *p) {
	if (*p == '"') return skip_str(p);
	if (*p == '{') return skip_container(p, '{', '}');
	if (*p == '[') return skip_container(p, '[', ']');
	if (!strncmp(p, "true", 4)) return p + 4;
	if (!strncmp(p, "false", 5)) return p + 5;
	if (!strncmp(p, "null", 4)) return p + 4;
	if (*p == '-' || isdigit((unsigned char) *p)) {
		char *end;
		strtod(p, &end);
		return end > p ? end : NULL;
	}
	return NULL;
}

bool hj_valid_object(const char *json) {
	if (!json) return false;
	const char *p = ws(json);
	if (*p != '{') return false;
	p = skip_container(p, '{', '}');
	return p && *ws(p) == '\0';
}

/* Finds the value of a top-level key; returns a pointer to it or NULL. */
static const char *find(const char *json, const char *k) {
	if (!hj_valid_object(json)) return NULL;
	size_t klen = strlen(k);
	const char *p = ws(ws(json) + 1);
	while (*p == '"') {
		const char *kstart = p + 1, *kend = skip_str(p);
		bool match = (size_t) (kend - 1 - kstart) == klen && !strncmp(kstart, k, klen);
		p = ws(kend);
		p = ws(p + 1); /* ':' */
		if (match) return p;
		p = ws(skip_value(p));
		if (*p == ',') p = ws(p + 1);
	}
	return NULL;
}

bool hj_get_str(const char *json, const char *k, char *out, size_t n) {
	const char *p = find(json, k);
	if (!p || *p != '"' || !n) return false;
	size_t i = 0;
	for (p++; *p && *p != '"'; p++) {
		char c = *p;
		if (c == '\\') {
			c = *++p;
			if (c == 'n') c = '\n';
			else if (c == 't') c = '\t';
			else if (c == 'u') { /* keep it simple: non-ASCII escapes become '?' */
				c = '?';
				for (int d = 0; d < 4 && p[1]; d++) p++;
			}
		}
		if (i + 1 < n) out[i++] = c;
	}
	out[i] = '\0';
	return true;
}

bool hj_get_num(const char *json, const char *k, double *out) {
	const char *p = find(json, k);
	if (!p || !(*p == '-' || isdigit((unsigned char) *p))) return false;
	/* strtod also reads -nan, -inf and 1e999, which no JSON number is and
	 * which no caller's range check catches (NaN fails every comparison) */
	double v = strtod(p, NULL);
	if (!isfinite(v)) return false;
	*out = v;
	return true;
}

bool hj_num_bad(const char *json, const char *k) {
	const char *p = find(json, k);
	return p && (*p == '-' || isdigit((unsigned char) *p)) && !isfinite(strtod(p, NULL));
}

bool hj_get_bool(const char *json, const char *k, bool *out) {
	const char *p = find(json, k);
	if (!p) return false;
	if (!strncmp(p, "true", 4)) *out = true;
	else if (!strncmp(p, "false", 5)) *out = false;
	else return false;
	return true;
}

int hj_get_num_array(const char *json, const char *k, double *out, int max) {
	const char *p = find(json, k);
	if (!p || *p != '[') return -1;
	int n = 0;
	p = ws(p + 1);
	if (*p == ']') return 0;
	for (;;) {
		char *end;
		if (!(*p == '-' || isdigit((unsigned char) *p))) return -1;
		double v = strtod(p, &end);
		if (!isfinite(v)) return -1;
		if (n < max) out[n] = v;
		n++;
		p = ws(end);
		if (*p == ']') break;
		if (*p++ != ',') return -1;
		p = ws(p);
	}
	return n < max ? n : max;
}

bool hj_get_raw(const char *json, const char *k, const char **val, size_t *len) {
	const char *p = find(json, k), *end;
	if (!p || !(end = skip_value(p))) return false;
	*val = p;
	*len = (size_t) (end - p);
	return true;
}

bool hj_get_str_span(const char *json, const char *k, const char **val, size_t *len) {
	const char *p;
	size_t n;
	if (!hj_get_raw(json, k, &p, &n) || *p != '"' || memchr(p, '\\', n)) return false;
	*val = p + 1;
	*len = n - 2;
	return true;
}
