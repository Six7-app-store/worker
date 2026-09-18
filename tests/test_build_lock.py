"""Tests for the Redis-backed PackerBuildLock service."""

import time
from unittest.mock import MagicMock

import pytest

from app.services import build_lock as build_lock_module
from app.services.build_lock import _LUA_RELEASE, _LUA_RENEW, PackerBuildLock


@pytest.fixture
def fake_redis(mocker):
    """Patch app.services.build_lock._redis_client to return a MagicMock fake."""
    fake = MagicMock()
    # Defaults: SET succeeds, pttl returns a sensible value, eval returns 1.
    fake.set.return_value = True
    fake.pttl.return_value = 30_000
    fake.eval.return_value = 1
    mocker.patch.object(build_lock_module, "_redis_client", return_value=fake)
    return fake


@pytest.fixture
def fast_lock(fake_redis):
    """Create a PackerBuildLock with a tiny heartbeat interval for tests."""
    return PackerBuildLock(
        project_id="proj-123",
        image_name="my-image",
        ttl_ms=10_000,
        heartbeat_interval_s=0.05,
        poll_interval_s=0,
        total_wait_s=60,
    )


@pytest.mark.unit
class TestKeyNaming:
    """Verifies the lock key naming convention."""

    def test_key_includes_project_id_and_image(self, fake_redis):
        """Key includes both project id and image name in the documented order."""
        lock = PackerBuildLock("my-proj", "ubuntu-22")
        assert lock.key == "lock:packer:my-proj:ubuntu-22"

    def test_key_uses_unknown_when_project_id_is_none(self, fake_redis):
        """Key falls back to 'unknown' when project_id is None."""
        lock = PackerBuildLock(None, "img")  # type: ignore[arg-type]
        assert lock.key == "lock:packer:unknown:img"

    def test_key_uses_unknown_when_project_id_is_empty(self, fake_redis):
        """Key falls back to 'unknown' when project_id is an empty string."""
        lock = PackerBuildLock("", "img")
        assert lock.key == "lock:packer:unknown:img"

    def test_token_is_unique_per_instance(self, fake_redis):
        """Each lock instance gets a unique token."""
        a = PackerBuildLock("p", "i")
        b = PackerBuildLock("p", "i")
        assert a.token != b.token


@pytest.mark.unit
class TestAcquireOrWait:
    """Verifies the acquire_or_wait polling behavior."""

    def test_acquire_returns_true_when_set_succeeds(self, fast_lock, fake_redis):
        """acquire_or_wait returns True when SET NX PX succeeds and marks the lock held."""
        fake_redis.set.return_value = True

        result = fast_lock.acquire_or_wait()

        assert result is True
        assert fast_lock._held is True
        fake_redis.set.assert_called_once_with(fast_lock.key, fast_lock.token, nx=True, px=fast_lock.ttl_ms)
        fast_lock.release()

    def test_acquire_starts_heartbeat_thread(self, fast_lock, fake_redis):
        """Acquiring the lock spawns a daemon heartbeat thread with the documented name prefix."""
        fake_redis.set.return_value = True

        fast_lock.acquire_or_wait()

        thread = fast_lock._heartbeat
        assert thread is not None
        assert thread.daemon is True
        assert thread.name.startswith("packer-lock-heartbeat-")
        fast_lock.release()

    def test_acquire_returns_false_when_set_fails_within_deadline(self, fast_lock, fake_redis):
        """acquire_or_wait returns False and sleeps when SET fails but deadline isn't reached."""
        fake_redis.set.return_value = False
        fake_redis.pttl.return_value = 1234

        result = fast_lock.acquire_or_wait()

        assert result is False
        assert fast_lock._held is False
        fake_redis.pttl.assert_called_once_with(fast_lock.key)

    def test_acquire_sleeps_poll_interval_when_waiting(self, fake_redis, mocker):
        """acquire_or_wait sleeps for poll_interval_s when waiting on another holder."""
        fake_redis.set.return_value = False
        sleep_mock = mocker.patch.object(build_lock_module.time, "sleep")
        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=7, total_wait_s=60)

        lock.acquire_or_wait()

        sleep_mock.assert_called_once_with(7)

    def test_acquire_raises_timeout_when_deadline_passed(self, fake_redis):
        """acquire_or_wait raises TimeoutError when SET fails AND the deadline is past."""
        fake_redis.set.return_value = False
        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=0, total_wait_s=60)
        # Force the deadline into the past.
        lock.deadline = time.monotonic() - 1

        with pytest.raises(TimeoutError) as exc_info:
            lock.acquire_or_wait()

        assert lock.key in str(exc_info.value)

    def test_acquire_logs_none_when_pttl_negative(self, fake_redis, mocker):
        """When pttl returns -2 (expired) or -1 (no TTL), the log helper resolves to None."""
        fake_redis.set.return_value = False
        fake_redis.pttl.return_value = -2
        mocker.patch.object(build_lock_module.time, "sleep")
        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=0, total_wait_s=60)

        result = lock.acquire_or_wait()

        assert result is False


@pytest.mark.unit
class TestRelease:
    """Verifies the release semantics."""

    def test_release_is_noop_when_not_held(self, fake_redis):
        """release() does nothing (no eval) when the lock was never acquired."""
        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05)

        lock.release()

        fake_redis.eval.assert_not_called()

    def test_release_after_acquire_calls_lua_release(self, fast_lock, fake_redis):
        """release() runs the Lua release script with the lock key and token."""
        fake_redis.set.return_value = True
        fast_lock.acquire_or_wait()
        # Reset to ignore any heartbeat eval that may have run.
        fake_redis.eval.reset_mock()

        fast_lock.release()

        # Find at least one call matching the release script signature.
        release_calls = [c for c in fake_redis.eval.call_args_list if c.args and c.args[0] == _LUA_RELEASE]
        assert release_calls, "Expected _LUA_RELEASE eval"
        call = release_calls[0]
        assert call.args == (_LUA_RELEASE, 1, fast_lock.key, fast_lock.token)
        assert fast_lock._held is False

    def test_release_swallows_redis_exception_and_resets_held(self, fast_lock, fake_redis):
        """release() catches exceptions from eval and still flips _held to False."""
        fake_redis.set.return_value = True
        fast_lock.acquire_or_wait()

        # Make the LUA_RELEASE eval raise; heartbeat may have already exited.
        fake_redis.eval.side_effect = RuntimeError("redis is down")

        fast_lock.release()  # must not raise

        assert fast_lock._held is False

    def test_release_joins_heartbeat_and_clears_reference(self, fast_lock, fake_redis):
        """release() stops the heartbeat thread and clears the _heartbeat reference."""
        fake_redis.set.return_value = True
        fast_lock.acquire_or_wait()
        thread = fast_lock._heartbeat
        assert thread is not None

        fast_lock.release()

        # Give the daemon a moment to wind down; join was already called inside release.
        assert fast_lock._heartbeat is None
        assert not thread.is_alive()


@pytest.mark.unit
class TestHeartbeat:
    """Verifies the heartbeat loop."""

    def test_heartbeat_renews_via_lua(self, fast_lock, fake_redis):
        """Heartbeat loop calls eval(_LUA_RENEW, ...) with key, token, and ttl_ms."""
        fake_redis.set.return_value = True
        fake_redis.eval.return_value = 1

        fast_lock.acquire_or_wait()
        time.sleep(0.2)  # let at least one heartbeat tick fire
        fast_lock.release()

        renew_calls = [c for c in fake_redis.eval.call_args_list if c.args and c.args[0] == _LUA_RENEW]
        assert renew_calls, "Expected at least one _LUA_RENEW eval"
        # Each renew call: (script, 1, key, token, ttl_ms)
        first = renew_calls[0]
        assert first.args == (_LUA_RENEW, 1, fast_lock.key, fast_lock.token, fast_lock.ttl_ms)

    def test_heartbeat_continues_on_redis_exception(self, fake_redis, mocker):
        """Heartbeat warns and continues looping when eval raises."""
        fake_redis.set.return_value = True
        # First call raises, subsequent calls return 1 (renewed). Then we stop.
        fake_redis.eval.side_effect = [RuntimeError("blip"), 1, 1, 1, 1]

        warn_spy = mocker.spy(build_lock_module.logger, "warning")

        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=0, total_wait_s=60)
        lock.acquire_or_wait()
        time.sleep(0.25)
        # Lock should still be considered held since renewals after the blip succeeded.
        assert lock._held is True
        lock.release()

        # The "raised" warning should have been logged.
        warned_messages = [str(c.args[0]) if c.args else "" for c in warn_spy.call_args_list]
        assert any("raised" in m for m in warned_messages)

    def test_heartbeat_exits_when_renew_returns_zero(self, fake_redis, mocker):
        """Heartbeat exits and unsets _held when renew returns 0 (lock vanished)."""
        fake_redis.set.return_value = True
        fake_redis.eval.return_value = 0  # renew returns 0 -> lock gone

        warn_spy = mocker.spy(build_lock_module.logger, "warning")

        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=0, total_wait_s=60)
        lock.acquire_or_wait()
        time.sleep(0.2)

        assert lock._held is False
        assert lock._heartbeat is not None
        # Give the thread a moment to exit cleanly.
        lock._heartbeat.join(timeout=1)
        assert not lock._heartbeat.is_alive()

        warned_messages = [str(c.args[0]) if c.args else "" for c in warn_spy.call_args_list]
        assert any("vanished" in m for m in warned_messages)

        # release() should be a no-op here because _held was flipped off.
        fake_redis.eval.reset_mock()
        lock.release()
        release_calls = [c for c in fake_redis.eval.call_args_list if c.args and c.args[0] == _LUA_RELEASE]
        assert not release_calls


@pytest.mark.unit
class TestContextManager:
    """Verifies context manager behavior."""

    def test_enter_returns_self(self, fast_lock):
        """__enter__ returns the lock instance itself."""
        assert fast_lock.__enter__() is fast_lock

    def test_exit_calls_release(self, fast_lock, fake_redis, mocker):
        """__exit__ delegates to release()."""
        release_spy = mocker.patch.object(fast_lock, "release")

        fast_lock.__exit__(None, None, None)

        release_spy.assert_called_once_with()

    def test_with_block_releases_on_exit(self, fake_redis):
        """Using the lock as a context manager releases it cleanly on block exit."""
        fake_redis.set.return_value = True
        lock = PackerBuildLock("p", "i", heartbeat_interval_s=0.05, poll_interval_s=0, total_wait_s=60)

        with lock as acquired:
            assert acquired is lock
            assert lock.acquire_or_wait() is True
            assert lock._held is True

        assert lock._held is False


@pytest.mark.unit
class TestRedisClientFactory:
    """The one place that decides which Redis the lock lives in.

    The lock is only correct if every worker talks to the *same* Redis -
    a second instance would hand out the same lock twice and let two
    Packer builds race on the same image. So the factory must read the
    configured backend URL and nothing else.
    """

    def test_redis_client_uses_the_configured_backend_url(self, mocker):
        """``_redis_client`` passes CELERY_RESULT_BACKEND straight to from_url."""
        mocker.patch.object(
            build_lock_module.settings,
            "CELERY_RESULT_BACKEND",
            "redis://configured-host:6379/7",
        )
        from_url = mocker.patch.object(build_lock_module.redis.Redis, "from_url")

        client = build_lock_module._redis_client()

        from_url.assert_called_once_with("redis://configured-host:6379/7")
        assert client is from_url.return_value
