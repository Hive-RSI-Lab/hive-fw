/*
 * Tiny JSON writer and flat-object reader for the test API.
 *
 * The writer appends into a caller's buffer and never overflows it (it sets
 * `overflow` instead). The reader handles what the API's request bodies are:
 * one flat object of strings, numbers, booleans and nulls; nested values are
 * skipped.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#define HJ_MAX_DEPTH 8

typedef struct {
	char *buf;
	size_t cap, len;
	int depth;
	bool first[HJ_MAX_DEPTH];
	bool overflow;
} hj_t;

void hj_init(hj_t *j, char *buf, size_t cap);
/* key is NULL at the root and inside arrays */
void hj_obj(hj_t *j, const char *key);
void hj_obj_end(hj_t *j);
void hj_arr(hj_t *j, const char *key);
void hj_arr_end(hj_t *j);
void hj_str(hj_t *j, const char *key, const char *val);       /* NULL -> null */
void hj_num(hj_t *j, const char *key, double val, int decimals); /* NaN -> null */
void hj_int(hj_t *j, const char *key, long long val);
void hj_bool(hj_t *j, const char *key, bool val);
void hj_null(hj_t *j, const char *key);

/* Reader: each returns false if the key is missing or of another type. */
bool hj_get_str(const char *json, const char *key, char *out, size_t n);
bool hj_get_num(const char *json, const char *key, double *out);
bool hj_get_bool(const char *json, const char *key, bool *out);
/* true if `json` is one well-formed object (whitespace around it allowed) */
bool hj_valid_object(const char *json);
/* A top-level array of numbers; returns how many were read (at most max), or
 * -1 if the key is missing, isn't an array, or holds a non-number. */
int hj_get_num_array(const char *json, const char *key, double *out, int max);
/* The raw text of a top-level key's value (an object, array, string with its
 * quotes, number...), without copying it: for a nested body or a long
 * base64 string. */
bool hj_get_raw(const char *json, const char *key, const char **val, size_t *len);
/* The same, for a value known to be one string: its characters between the
 * quotes, unescaped only if it has no escapes (base64 never does). */
bool hj_get_str_span(const char *json, const char *key, const char **val, size_t *len);
