// fw-src: @DIR@/@NAME@.c
// fw-inc: @DIR@/include
#include "@NAME@.h"
#include "unity.h"

void setUp(void) {}
void tearDown(void) {}

static void test_scale(void) {
	TEST_ASSERT_EQUAL_UINT32(0, @NAME@_scale(-5, 1000));
	TEST_ASSERT_EQUAL_UINT32(500, @NAME@_scale(50, 1000));
	TEST_ASSERT_EQUAL_UINT32(1000, @NAME@_scale(150, 1000));
	TEST_ASSERT_EQUAL_UINT32(4294967295u, @NAME@_scale(100, 4294967295u));
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_scale);
	return UNITY_END();
}
