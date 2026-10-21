"""Run the full test suite with test modules in parallel processes.

Usage: .venv/bin/python scripts/run_tests.py [-j 4]

Each test module runs in its own process (the same `unittest discover` the maintained command uses), slowest
first, so independent modules overlap. Exit status is non-zero if any module fails.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
SLOW_FIRST = ['test_trade_packages', 'test_advisor', 'test_matchups', 'test_trades', 'test_trade_market',
              'test_yahoo_snapshot', 'test_in_season', 'test_audit', 'test_deterministic', 'test_schedule']


def run(module):
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(TESTS), '-p', module + '.py'],
                          capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
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
