"""Fail closed on kernel storage/lockup alarms in formal performance runs."""
import re
import subprocess
from pathlib import Path

_ALARM = re.compile(
    r"(?:\bnvme\b.*(?:\btimeout\b|\breset(?:ting)?\b|Abort status))"
    r"|(?:\bBUG:|\bWARNING:|hard LOCKUP|soft lockup|rcu.*stall)", re.IGNORECASE
)

def alarm_lines(log):
    return [line for line in log.splitlines() if _ALARM.search(line)]

class KernelHealthGuard:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.check('before')

    def check(self, stage='after'):
        # Read failure is itself fatal: absence of evidence is not kernel health.
        log = subprocess.run(['dmesg'], check=True, capture_output=True, text=True).stdout
        (self.directory / ('kernel-' + stage + '.log')).write_text(log)
        alarms = alarm_lines(log)
        if alarms:
            (self.directory / 'kernel-alarm.log').write_text('\n'.join(alarms) + '\n')
            raise RuntimeError('kernel health gate: storage/lockup alarm in current boot; '
                               'performance run is ineligible; see kernel-alarm.log')
