// fw-src: main/volume.c
// fw-inc: main
#include "unity.h"
#include "volume.h"

void setUp(void) {}
void tearDown(void) {}

static void test_ends(void)
{
    TEST_ASSERT_EQUAL_UINT8(192, volume_to_attenuation(0));
    TEST_ASSERT_EQUAL_UINT8(192, volume_to_attenuation(-5));
    TEST_ASSERT_EQUAL_UINT8(0, volume_to_attenuation(100));
    TEST_ASSERT_EQUAL_UINT8(0, volume_to_attenuation(150));
}

static void test_monotonic(void)
{
    for (int p = 1; p < 100; p++) {
        TEST_ASSERT_TRUE(volume_to_attenuation(p) >= volume_to_attenuation(p + 1));
    }
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_ends);
    RUN_TEST(test_monotonic);
    return UNITY_END();
}
