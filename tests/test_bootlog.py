import unittest

from tests import _util  # noqa: F401
import bootlog

IDF5_OK = """ets Jun  8 2016 00:22:57

rst:0x1 (POWERON_RESET),boot:0x12 (SPI_FAST_FLASH_BOOT)
I (29) boot: ESP-IDF v5.5.6 2nd stage bootloader
I (312) main_task: Calling app_main()
I (312) hello: tick 0
"""
IDF4_OK = """rst:0x1 (POWERON_RESET),boot:0x12 (SPI_FAST_FLASH_BOOT)
I (602) cpu_start: Starting scheduler on PRO CPU.
I (0) cpu_start: Starting scheduler on APP CPU.
"""


class Judge(unittest.TestCase):
    def test_idf5_and_idf4_boots_pass(self):
        self.assertTrue(bootlog.judge(IDF5_OK).ok)
        self.assertTrue(bootlog.judge(IDF4_OK).ok)

    def test_panic_fails_even_after_app_main(self):
        v = bootlog.judge(IDF5_OK + "Guru Meditation Error: Core  0 panic'ed (LoadProhibited)\n")
        self.assertFalse(v.ok)
        self.assertIn("LoadProhibited", v.reason)
        self.assertTrue(v.reached)

    def test_reboot_loop_fails(self):
        log = IDF4_OK + "rst:0xc (SW_CPU_RESET),boot:0x12\n" + IDF4_OK
        v = bootlog.judge(log)
        self.assertFalse(v.ok)
        self.assertIn("SW_CPU_RESET", v.reason)

    def test_custom_expect(self):
        self.assertTrue(bootlog.judge(IDF5_OK, r"hello: tick 0").ok)
        v = bootlog.judge(IDF5_OK, r"hello: tick 5")
        self.assertFalse(v.ok)
        self.assertIn("never reached", v.reason)

    def test_running_check_waits(self):
        v = bootlog.judge("rst:0x1 (POWERON_RESET)\nI (29) boot: ...\n", done=False)
        self.assertEqual(v.reason, "still booting")

    def test_empty_and_blank_flash(self):
        self.assertIn("no serial output", bootlog.judge("").reason)
        self.assertIn("no valid image", bootlog.judge("invalid header: 0xffffffff\n").reason)

    def test_errorcheck_and_wdt(self):
        self.assertFalse(bootlog.judge(IDF5_OK + "ESP_ERROR_CHECK failed: esp_err_t 0x103\n").ok)
        self.assertFalse(bootlog.judge(IDF5_OK + "E (5312) task_wdt: Task watchdog got triggered.\n").ok)


if __name__ == "__main__":
    unittest.main()
