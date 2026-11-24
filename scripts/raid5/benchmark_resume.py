"""Audit immutable completed cells and plan a continuation without replaying them."""
import hashlib
import json
from pathlib import Path
from benchmark_protocol import VERSION, ranges, validate

HARNESS_CHANGES = {'scripts/raid5/' + n for n in [
    'benchmark.py', 'benchmark_report.py', 'benchmark_protocol.py',
    'test_benchmark_protocol.py', 'benchmark_resume.py', 'test_benchmark_resume.py']}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def tree_hashes(directory):
    return {str(p.relative_to(directory)): digest(p) for p in sorted(directory.rglob('*')) if p.is_file()}


def pending(cells, repeats, done, recovery_done, recovery):
    """One stable key per original repeat/target/cell; never renumber repeats."""
    work = []
    for repeat in range(repeats):
        for target in (['rublk', 'md'] if repeat % 2 == 0 else ['md', 'rublk']):
            for queues in [4, 1]:
                keys = [(repeat, target, i) for i, cell in enumerate(cells)
                        if cell['jobs'] == queues and (repeat, target, i) not in done]
                need_recovery = recovery and queues == 4 and (repeat, target) not in recovery_done
                if keys or need_recovery:
                    work.append({'repeat': repeat, 'target': target, 'queues': queues,
                                 'cells': [key[2] for key in keys], 'recovery': need_recovery})
    return work


def audit(parent, current):
    parent = Path(parent).resolve()
    manifest_path = parent / 'manifest.json'
    old = read(manifest_path)
    if old.get('resume_provenance'):verify_imports(old['resume_provenance'])
    for k in ['protocol', 'cells', 'capacity', 'members', 'binary_sha256',
              'program_sha256', 'host_boot_id', 'member_queue_configuration',
              'warmup_seconds', 'measurement_seconds', 'repeats', 'mode']:
        assert old[k] == current[k], 'incompatible parent: ' + k
    assert old['protocol'] == VERSION and not old['pilot'] and not old.get('protocol_check')
    assert old['harness_arguments']['recovery_rate'] == current['harness_arguments']['recovery_rate']
    for k in ['recovery_duration', 'recovery_band', 'recovery_pause_seconds', 'recovery_pause_after']:
        assert old['harness_arguments'][k] == current['harness_arguments'][k], k
    before, after = old['full_source_sha256'], current['full_source_sha256']
    changes = {k: {'before': before.get(k), 'after': after.get(k)}
               for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert set(changes) <= HARNESS_CHANGES, 'runtime/config source changed: ' + repr(set(changes) - HARNESS_CHANGES)
    for name in ['kernel-before.log', 'kernel-after.log']:
        from kernel_health import alarm_lines
        assert not alarm_lines((parent / name).read_text()), 'parent kernel alarm'
    assert not (parent / 'PERFORMANCE_INELIGIBLE.json').exists()
    assert not (parent / 'kernel-alarm.log').exists()
    records, done, recovery_done = [], set(), set()

    def keep(name, kind, key, queues):
        directory = parent / name
        prior = {x['name']:x for x in old.get('resume_provenance',{}).get('records',[])}.get(name)
        proof = Path(prior['parity_proof']) if prior else parent / f'{key[1]}-physical-{key[0]}-q{queues}.log'
        check = read(proof)
        assert check['verified_stripes'] == 32752 and check['parity_errors'] == 0
        records.append({'name': name, 'kind': kind, 'key': list(key),
                        'directory': str(directory.resolve()), 'files': tree_hashes(directory),
                        'parity_proof': str(proof), 'parity_sha256': digest(proof)})

    assert len(old['results']) == len(set(old['results']))
    for name in old['results']:
        assert Path(name).name == name, 'unexpected result path'
        d = parent / name
        row = read(d / 'metrics.json');key = (row['repeat'], row['target'], row['cell'])
        assert key not in done and 0 <= key[0] < current['repeats'] and key[1] in ['rublk', 'md']
        cell = current['cells'][key[2]]
        assert row['configuration'] == cell and row['protocol'] == VERSION and not row['pilot']
        assert row['job_ranges'] == ranges(cell, current['capacity'])
        timing = read(d / 'timing.json');assert timing == row['timing']
        assert [x['phase'] for x in timing['phases']] == ['warmup', 'measurement']
        for phase, seconds in zip(timing['phases'], [current['warmup_seconds'], current['measurement_seconds']]):
            assert phase['complete'] and phase['duration_seconds'] == seconds
            command = read(d / (phase['phase'] + '-command.json'))
            assert command == phase['command'] and '--ramp_time=0' in command and '--group_reporting=0' in command
            assert '--runtime=' + str(seconds) in command
            raw = read(d / (phase['phase'] + '.json'))
            validate(raw, cell['jobs'], seconds)
            if phase['phase'] == 'measurement':assert raw == row['fio'] == read(d / 'fio.json')
        keep(name, 'steady', key, cell['jobs']);done.add(key)
    assert len(old.get('recovery_results', [])) == len(set(old.get('recovery_results', [])))
    for name in old.get('recovery_results', []):
        d = parent / name;report = read(d / 'report.json');parts = name.split('-')
        key = (int(parts[0]), parts[1]);assert key not in recovery_done
        assert 0 <= key[0] < current['repeats'] and key[1] in ['rublk', 'md']
        assert report['target'] == key[1] and report['scan_activity_observed']
        assert all(j['error'] == 0 for j in read(d / 'fio.json')['jobs'])
        keep(name, 'recovery', key, 4);recovery_done.add(key)
    evidence = {'parent': str(parent), 'manifest_sha256': digest(manifest_path),
                'harness_changes': changes, 'records': records,
                'steady_count': len(done), 'recovery_count': len(recovery_done),
                'scope': 'completed keys only; failed/incomplete cell excluded; recovery windows retain original qualification'}
    return evidence, done, recovery_done


def install(evidence, out, manifest):
    for record in evidence['records']:
        (out / record['name']).symlink_to(record['directory'], target_is_directory=True)
        field = 'results' if record['kind'] == 'steady' else 'recovery_results'
        manifest.setdefault(field, []).append(record['name'])
    manifest['resume_provenance'] = evidence


def verify_imports(evidence):
    parent = Path(evidence['parent'])
    assert digest(parent / 'manifest.json') == evidence['manifest_sha256'], 'parent manifest changed'
    for r in evidence['records']:
        assert tree_hashes(Path(r['directory'])) == r['files'], 'imported evidence changed: ' + r['name']
        assert digest(r['parity_proof']) == r['parity_sha256'], 'parity proof changed'
