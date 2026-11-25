"""Steady-state protocol v2: disjoint job ranges, explicit bounded phases."""
import json
import time

VERSION = 'disjoint-jobs-explicit-phases-v2'
VALIDATION_REVISION = 'bounded-completion-tail-v3'
START_SKEW_MS = 1000
DRAIN_ALLOWANCE_SECONDS = 60


def ranges(cell, capacity):
    jobs = cell['jobs']
    size = 131072 if cell['locality'] == 'hotspot' else capacity // jobs // 131072 * 131072
    result = [{'job': i, 'offset': i * size, 'bytes': size} for i in range(jobs)]
    assert size > 0 and jobs * size <= capacity
    return result


def validate(result, jobs, seconds):
    rows = result['jobs']
    assert len(rows) == jobs, 'per-job statistics required; never group_reporting'
    assert all(j['error'] == 0 for j in rows), 'fio client error'
    starts = [j['job_start'] for j in rows]
    skew = max(starts) - min(starts)
    assert skew <= START_SKEW_MS, f'job start skew {skew}ms exceeds {START_SKEW_MS}ms'
    evidence = []
    for j in rows:
        runtime = max(j[d]['runtime'] for d in ['read', 'write'])
        assert runtime >= seconds * 1000, f'job {j["jobname"]} measured only {runtime}ms'
        assert runtime <= (seconds + DRAIN_ALLOWANCE_SECONDS) * 1000, 'unbounded completion drain'
        evidence.append({'name': j['jobname'], 'start_unix_ms': j['job_start'],
                         'runtime_ms': runtime, 'runtime_overrun_ms': runtime - seconds * 1000,
                         'error': j['error']})
    ends = [x['start_unix_ms'] + x['runtime_ms'] for x in evidence]
    end_skew = max(ends) - min(ends)
    return {'start_skew_ms': skew, 'end_skew_ms': end_skew, 'end_skew_is_error': False,
            'validation_revision': VALIDATION_REVISION, 'runtime_includes_completion_tail': True,
            'submission_stop_not_observed': True, 'jobs': evidence}


def run_phases(base, directory, name, warmup, measure, jobs, command):
    """command owns timeout cancellation, logging, and kernel-health checks."""
    phases = []
    for phase, seconds in [('warmup', warmup), ('measurement', measure)]:
        prefix = directory / phase
        cmd = [*base, '--ramp_time=0', '--runtime=' + str(seconds),
               '--group_reporting=0', '--output-format=json+',
               '--output=' + str(prefix) + '.json',
               '--write_hist_log=' + str(prefix) + '-latency', '--log_hist_msec=1000',
               '--log_unix_epoch=1', '--write_bw_log=' + str(prefix) + '-bw',
               '--log_avg_msec=1000']
        (directory / (phase + '-command.json')).write_text(json.dumps(cmd, indent=2))
        info = {'phase': phase, 'duration_seconds': seconds,
                'timeout_seconds': seconds + DRAIN_ALLOWANCE_SECONDS,
                'started_monotonic_ns': time.monotonic_ns(), 'command': cmd}
        phases.append(info)
        try:
            command(name + '-' + phase, cmd, timeout=info['timeout_seconds'])
            raw = prefix.with_suffix('.json').read_text()
            result = json.loads(raw[raw.index('{'):])
            info.update(validate(result, jobs, seconds))
            info['finished_monotonic_ns'] = time.monotonic_ns()
            info['complete'] = True
        except BaseException as e:
            info['complete'] = False
            info['error'] = repr(e)
            raise
        finally:
            (directory / 'timing.json').write_text(json.dumps({'protocol': VERSION,
                'separate_fio_processes': True, 'phases': phases}, indent=2))
    # Preserve the established report input name, containing measurement only.
    (directory / 'fio.json').write_text(json.dumps(result, indent=2))
    return result
