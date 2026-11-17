/* fw's heap squeeze, linked into a QEMU build that has fw.json
 * "qemu": {"scenarios": ...}.
 *
 * A fault step sets fw_heap_leave to the bytes of heap to leave free ({"set":
 * "fw_heap_leave = 16000"}); the next task to call vTaskDelay (fw build
 * --qemu links this file with -Wl,--wrap=vTaskDelay, and nearly every task
 * sleeps through it) takes the rest, in 512-byte blocks, from the heap
 * fw_heap_caps names (internal 8-bit RAM unless set). fw_heap_held says how
 * much it holds. Setting fw_heap_leave back to 0 frees it all. Built without
 * IDF headers.
 */
#include <stddef.h>

#define FW_CAP_8BIT (1u << 2)
#define FW_CAP_INTERNAL (1u << 11)
#define FW_BLOCK 512u

void *heap_caps_malloc(size_t size, unsigned caps);
void heap_caps_free(void *ptr);
size_t heap_caps_get_free_size(unsigned caps);
void __real_vTaskDelay(unsigned ticks);

volatile unsigned fw_heap_leave;
volatile unsigned fw_heap_caps = FW_CAP_INTERNAL | FW_CAP_8BIT;
volatile unsigned fw_heap_held;
static void **hog;
static volatile int busy;

void __wrap_vTaskDelay(unsigned ticks)
{
    /* one task at a time, either core: the others just sleep */
    if ((fw_heap_leave || hog) && __sync_bool_compare_and_swap(&busy, 0, 1)) {
        unsigned caps = fw_heap_caps;
        if (fw_heap_leave) {
            while (heap_caps_get_free_size(caps) > fw_heap_leave + 2 * FW_BLOCK) {
                void **b = heap_caps_malloc(FW_BLOCK, caps);
                if (!b) {
                    break;
                }
                *b = hog;
                hog = b;
                fw_heap_held += FW_BLOCK;
            }
        } else {
            while (hog) {
                void **next = *hog;
                heap_caps_free(hog);
                hog = next;
                fw_heap_held -= FW_BLOCK;
            }
        }
        busy = 0;
    }
    __real_vTaskDelay(ticks);
}
