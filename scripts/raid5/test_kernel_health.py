import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from kernel_health import KernelHealthGuard, alarm_lines

class KernelHealthTests(unittest.TestCase):
    def test_real_nvme_and_stall_alarms(self):
        lines = [
            '[21920.648567] nvme nvme2: I/O tag 715 opcode 0x0 QID 3 timeout, aborting req_op:FLUSH(2) size:0',
            '[21920.649063] nvme nvme2: Abort status: 0x0',
            '[9.0] nvme nvme0: resetting controller',
            '[9.0] watchdog: BUG: soft lockup - CPU#0 stuck',
            '[9.0] rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:',
        ]
        self.assertEqual(alarm_lines('\n'.join(lines)), lines)
        self.assertEqual(alarm_lines('[0] printk: debug: ignoring loglevel setting.\n[1] nvme nvme2: 8/0/0 default/read/poll queues\n[2] md: resync done'), [])

    def test_zero_status_abort_is_not_a_clean_completion(self):
        self.assertTrue(alarm_lines('nvme nvme2: Abort status: 0x0'))

    def test_preexisting_alarm_rejects_before_work(self):
        with tempfile.TemporaryDirectory() as d, patch('kernel_health.subprocess.run', return_value=SimpleNamespace(stdout='nvme nvme0: FLUSH timeout')):
            with self.assertRaisesRegex(RuntimeError, 'ineligible'):
                KernelHealthGuard(d)
            self.assertTrue((Path(d) / 'kernel-before.log').exists())
            self.assertTrue((Path(d) / 'kernel-alarm.log').exists())

    def test_new_alarm_after_successful_command_rejects(self):
        with tempfile.TemporaryDirectory() as d, patch('kernel_health.subprocess.run', side_effect=[SimpleNamespace(stdout='nvme queues ready'), SimpleNamespace(stdout='nvme nvme2: FLUSH timeout')]):
            guard = KernelHealthGuard(d)
            with self.assertRaises(RuntimeError):
                guard.check()
            self.assertIn('FLUSH timeout', (Path(d) / 'kernel-after.log').read_text())

    def test_unreadable_kernel_log_fails_closed(self):
        with tempfile.TemporaryDirectory() as d, patch('kernel_health.subprocess.run', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                KernelHealthGuard(d)
