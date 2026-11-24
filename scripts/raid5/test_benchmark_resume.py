import copy
import json
from pathlib import Path
import tempfile
import unittest
from benchmark_resume import audit, pending, install, verify_imports
from benchmark_protocol import VERSION, ranges


class ResumeTests(unittest.TestCase):
    def test_planner_skips_exact_keys_without_renumbering(self):
        cells = [{'jobs': 4}, {'jobs': 1}]
        done = {(0, 'rublk', 0), (0, 'rublk', 1), (0, 'md', 0)}
        tasks = pending(cells, 2, done, {(0, 'rublk')}, True)
        keys = {(x['repeat'], x['target'], i) for x in tasks for i in x['cells']}
        self.assertEqual(keys, {(r, t, i) for r in range(2) for t in ['md', 'rublk'] for i in range(2)} - done)
        self.assertNotIn((0, 'rublk', 4), [(x['repeat'], x['target'], x['queues']) for x in tasks])
        self.assertEqual(sum(x['recovery'] for x in tasks), 3)
        self.assertEqual(pending(cells, 1, {(0,t,i) for t in ['md','rublk'] for i in range(2)}, {(0,'md'),(0,'rublk')}, True), [])

    def fixture(self, root):
        cell = {'jobs': 1, 'locality': 'hotspot'}
        m = {'protocol': VERSION, 'cells': [cell], 'capacity': 131072, 'members': ['disk'],
             'binary_sha256': 'binary', 'program_sha256': {'fio': 'fio'}, 'host_boot_id': 'boot',
             'member_queue_configuration': {}, 'warmup_seconds': 1, 'measurement_seconds': 5,
             'repeats': 1, 'mode': 'steady-with-recovery', 'pilot': False,
             'full_source_sha256': {'src/raid5/queue.rs': 'runtime', 'scripts/raid5/benchmark_protocol.py': 'old'},
             'harness_arguments': dict(recovery_rate='0', recovery_duration=30, recovery_band='mixed', recovery_pause_seconds=0, recovery_pause_after=5),
             'results': ['0-md-0'], 'recovery_results': []}
        d = root/'0-md-0';d.mkdir();phases=[]
        for phase, seconds in [('warmup',1),('measurement',5)]:
            cmd = ['fio', '--ramp_time=0', '--group_reporting=0', '--runtime='+str(seconds)]
            raw = {'jobs': [{'jobname':'test', 'error':0, 'job_start':1000, 'read':{'runtime':0}, 'write':{'runtime':seconds*1000+2}}]}
            (d/(phase+'-command.json')).write_text(json.dumps(cmd))
            (d/(phase+'.json')).write_text(json.dumps(raw))
            phases.append({'phase':phase,'duration_seconds':seconds,'command':cmd,'complete':True})
        timing={'phases':phases};(d/'timing.json').write_text(json.dumps(timing));(d/'fio.json').write_text(json.dumps(raw))
        row={'repeat':0,'target':'md','cell':0,'configuration':cell,'protocol':VERSION,'pilot':False,'job_ranges':ranges(cell,131072),'timing':timing,'fio':raw}
        (d/'metrics.json').write_text(json.dumps(row));(root/'manifest.json').write_text(json.dumps(m))
        (root/'md-physical-0-q1.log').write_text(json.dumps({'verified_stripes':32752,'parity_errors':0}))
        for name in ['kernel-before.log','kernel-after.log']:(root/name).write_text('')
        current=copy.deepcopy(m);current['full_source_sha256']['scripts/raid5/benchmark_protocol.py']='new'
        return current

    def test_reuse_tamper_and_compatibility_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);current=self.fixture(root);e,done,rec=audit(root,current)
            self.assertEqual(done,{(0,'md',0)});self.assertEqual(rec,set())
            out=root/'out';out.mkdir();view={};install(e,out,view)
            self.assertTrue((out/'0-md-0').is_symlink());self.assertEqual(view['results'],['0-md-0']);verify_imports(e)
            child=copy.deepcopy(current);child.update(view)
            (out/'manifest.json').write_text(json.dumps(child))
            for name in ['kernel-before.log','kernel-after.log']:(out/name).write_text('')
            e2,done2,_=audit(out,current);self.assertEqual(done2,done);verify_imports(e2)
            for key in ['binary_sha256','host_boot_id']:
                wrong=copy.deepcopy(current);wrong[key]='changed'
                with self.subTest(key=key),self.assertRaises(AssertionError):audit(root,wrong)
            wrong=copy.deepcopy(current);wrong['full_source_sha256']['src/raid5/queue.rs']='changed'
            with self.assertRaisesRegex(AssertionError,'source changed'):audit(root,wrong)
            (root/'0-md-0/fio.json').write_text('{}')
            with self.assertRaisesRegex(AssertionError,'evidence changed'):verify_imports(e)
            with self.assertRaises(AssertionError):audit(root,current)

    def test_partial_phase_and_duplicate_results_are_not_imported(self):
        for mode in ['partial','duplicate','parity']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);current=self.fixture(root)
                if mode=='partial':
                    p=root/'0-md-0/timing.json';j=json.loads(p.read_text());j['phases'][1]['complete']=False;p.write_text(json.dumps(j))
                elif mode=='duplicate':
                    p=root/'manifest.json';j=json.loads(p.read_text());j['results']*=2;p.write_text(json.dumps(j))
                else:(root/'md-physical-0-q1.log').write_text('{"verified_stripes":32752,"parity_errors":1}')
                with self.assertRaises(AssertionError):audit(root,current)


if __name__ == '__main__':unittest.main()
