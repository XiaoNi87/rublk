import unittest
from benchmark_protocol import ranges, validate


class ProtocolTests(unittest.TestCase):
    def rows(self):
        return {'jobs': [{'jobname': str(i), 'job_start': 100000 + i * 10,
                         'error': 0, 'read': {'runtime': 0},
                         'write': {'runtime': 120001}} for i in range(4)]}

    def test_ranges(self):
        for jobs in [1, 4]:
            for locality in ['hotspot', 'disjoint']:
                r = ranges({'jobs': jobs, 'locality': locality}, 4292870144)
                for a, b in zip(r, r[1:]):
                    self.assertLessEqual(a['offset'] + a['bytes'], b['offset'])
                self.assertLessEqual(r[-1]['offset'] + r[-1]['bytes'], 4292870144)
                if locality == 'hotspot':
                    self.assertTrue(all(x['bytes'] == 131072 for x in r))

    def test_aligned_jobs(self):
        self.assertEqual(len(validate(self.rows(), 4, 120)['jobs']), 4)

    def test_old_delayed_job_is_rejected(self):
        r = self.rows()
        r['jobs'][0]['job_start'] += 120000
        with self.assertRaisesRegex(AssertionError, 'start skew'):
            validate(r, 4, 120)

    def test_incomplete_error_and_aggregated_jobs(self):
        for mode in ['short', 'error', 'aggregated', 'drain']:
            r = self.rows()
            if mode == 'short': r['jobs'][0]['write']['runtime'] = 59000
            if mode == 'error': r['jobs'][0]['error'] = 5
            if mode == 'aggregated': r['jobs'] = r['jobs'][:1]
            if mode == 'drain': r['jobs'][0]['write']['runtime'] = 190000
            with self.subTest(mode=mode), self.assertRaises(AssertionError):
                validate(r, 4, 120)

    def test_aligned_completion_drain_is_reported(self):
        r = self.rows()
        for j in r['jobs']: j['write']['runtime'] = 121250
        evidence = validate(r, 4, 120)
        self.assertTrue(all(j['runtime_overrun_ms'] == 1250 for j in evidence['jobs']))

    def test_unequal_bounded_completion_tail_is_diagnostic(self):
        r = self.rows()
        r['jobs'][0]['write']['runtime'] = 125000
        result = validate(r, 4, 120)
        self.assertGreater(result['end_skew_ms'], 1000)
        self.assertFalse(result['end_skew_is_error'])
        self.assertTrue(result['submission_stop_not_observed'])


if __name__ == '__main__':
    unittest.main()
