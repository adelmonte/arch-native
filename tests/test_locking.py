import multiprocessing
import time

import pytest

from arch_native.state import _queue_lock, acquire_daemon_lock, daemon_pid


def _hold_lock(config, seconds, ready):
    with _queue_lock(config):
        ready.set()
        time.sleep(seconds)


def test_lock_is_reentrant(config):
    with _queue_lock(config):
        with _queue_lock(config):
            pass
        with _queue_lock(config):
            pass


def test_lock_excludes_other_processes(config):
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_hold_lock, args=(config, 2, ready))
    p.start()
    ready.wait(5)
    with pytest.raises(RuntimeError):
        with _queue_lock(config, timeout=0.3):
            pass
    p.join()
    with _queue_lock(config, timeout=1):
        pass


def _hold_daemon(config, ready, done):
    f = acquire_daemon_lock(config)
    assert f is not None
    ready.set()
    done.wait(5)


def test_daemon_pid(config):
    assert daemon_pid(config) == 0
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    p = multiprocessing.Process(target=_hold_daemon, args=(config, ready, done))
    p.start()
    ready.wait(5)
    assert daemon_pid(config) == p.pid
    assert acquire_daemon_lock(config) is None
    done.set()
    p.join()
    assert daemon_pid(config) == 0
