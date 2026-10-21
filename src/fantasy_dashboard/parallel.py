"""Ordered map over forked worker processes, used by the Waiver Advisor and Trade Lab searches.

Workers inherit the prepared engine copy-on-write and return only results, so the output is identical to a
single-process run. Small jobs, one worker, or a platform without fork run in this process.
"""

from __future__ import annotations

import multiprocessing
from time import perf_counter
import warnings

_JOB = None   # (function, items) inherited by forked workers; never pickled


def _work(bounds):
    fn, items = _JOB
    return [fn(items[k]) for k in range(*bounds)]


def parallel_map(fn, items, workers, *, min_items=200, min_chunk=20, chunks_per_worker=6, deadline=None):
    """[fn(item) for item in items], in order. With a `deadline` (perf_counter time) evaluation stops once it
    passes and a prefix of the results is returned."""
    global _JOB
    if workers <= 1 or len(items) < min_items or 'fork' not in multiprocessing.get_all_start_methods():
        out = []
        for item in items:
            if deadline is not None and perf_counter() > deadline:
                break
            out.append(fn(item))
        return out
    size = max(min_chunk, len(items) // (workers * chunks_per_worker))
    chunks = [(k, min(k + size, len(items))) for k in range(0, len(items), size)]
    _JOB = (fn, items)
    out = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)   # fork from a threaded server: workers only compute
            with multiprocessing.get_context('fork').Pool(workers) as pool:
                for part in pool.imap(_work, chunks):
                    out.extend(part)
                    if deadline is not None and perf_counter() > deadline:
                        pool.terminate()
                        break
        return out
    except (OSError, RuntimeError):
        return out + [fn(items[k]) for k in range(len(out), len(items))]
    finally:
        _JOB = None
