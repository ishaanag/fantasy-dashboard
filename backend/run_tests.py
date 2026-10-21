"""Run the full test suite with test modules in parallel processes.

Usage: PYTHONPATH=backend .venv/bin/python backend/run_tests.py [-j 4]

Each test module runs in its own process (the same `unittest discover` the maintained command uses), slowest
first, so independent modules overlap. Exit status is non-zero if any module fails.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent / 'tests'
SLOW_FIRST = ['test_trades_4b', 'test_trades_4d', 'test_advisor', 'test_trades', 'test_phase2', 'test_waiver', 'test_yahoo_snapshot',
              'test_phase3f', 'test_audit', 'test_schedule', 'test_deterministic']


def run(module):
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(TESTS), '-p', module + '.py'],
                          capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': str(TESTS.parent)})
    ran = re.search(r'Ran (\d+) test', proc.stderr)
    return module, proc.returncode, int(ran.group(1)) if ran else 0, time.perf_counter() - started, proc.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=4)
    args = parser.parse_args()
    modules = sorted((p.stem for p in TESTS.glob('test_*.py')),
                     key=lambda m: (SLOW_FIRST.index(m) if m in SLOW_FIRST else len(SLOW_FIRST), m))
    started, failed, total = time.perf_counter(), [], 0
    with ThreadPoolExecutor(args.jobs) as pool:
        for module, code, ran, seconds, err in pool.map(run, modules):
            total += ran
            print(f"{'ok  ' if code == 0 else 'FAIL'} {module:28} {ran:3} tests {seconds:6.1f}s", flush=True)
            if code:
                failed.append(module)
                print(err[-4000:])
    print(f"\n{total} tests in {len(modules)} modules, {time.perf_counter() - started:.0f}s; "
          f"{'all passed' if not failed else 'FAILED: ' + ', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
