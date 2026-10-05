"""Branch-coverage tests for worker/app/tasks.py.

These tests mock every external boundary (TofuExecutor, PerTaskCloudsConfig,
git_service, OpenStackService) so each test exercises one decision branch of
the real task code without touching processes or the network.

Celery tasks declared with ``bind=True`` are invoked here via ``task.run(...)``
— that's a method bound to the Task instance, so ``self`` inside the task
body is the real Celery task object. We patch ``task.send_event`` on a
per-test basis so the event bus is silenced.
"""

import json
import os

import pytest

from app.tasks import (
    _PHASES_DEPLOY,
    Failure,
    _build_current_roster,
    _looks_like_file_var_value,
    _PhaseTracker,
    _reconcile_scoped_vars_to_roster,
    _scrub_nested_nones,
    _strip_file_vars,
    _tfstate_schema_name,
    collect_tofu_outputs_helper,
    collect_tofu_state_helper,
    deploy_application,
    destroy_deployment,
    encode_tofu_vars,
    pause_deployment,
    redeploy_resource,
    resume_deployment,
)

# --- Helpers ----------------------------------------------------------------


def _silence_events(mocker, task):
    """Patch ``task.send_event`` so the broker/event bus is not touched."""
    mocker.patch.object(task, "send_event", return_value=None)


def _make_clouds_config_mock(mocker):
    """Patch ``PerTaskCloudsConfig`` so __enter__ returns a fake env mapping."""
    cc = mocker.MagicMock()
    cc.__enter__ = mocker.MagicMock(return_value={"OS_CLOUD": "test"})
    cc.__exit__ = mocker.MagicMock(return_value=None)
    cls = mocker.patch("app.tasks.PerTaskCloudsConfig", return_value=cc)
    return cls, cc


def _make_tofu_executor_mock(mocker, *, init=True, plan=True, apply_=True, destroy=True, state="{}", output=None):
    """Patch ``TofuExecutor`` so every method returns the supplied stub.

    The same instance is returned for every constructor call so the test can
    inspect call args across the whole task body.
    """
    inst = mocker.MagicMock()
    inst.init.return_value = (init, "init-stdout", "init-stderr")
    inst.plan.return_value = (plan, "plan-stdout", "plan-stderr")
    inst.apply.return_value = (apply_, "apply-stdout", "apply-stderr")
    inst.destroy.return_value = (destroy, "destroy-stdout", "destroy-stderr")
    inst.state_pull.return_value = state
    inst.output.return_value = output if output is not None else {"ip": "1.2.3.4"}
    cls = mocker.patch("app.tasks.TofuExecutor", return_value=inst)
    return cls, inst


def _make_openstack_service_mock(mocker):
    inst = mocker.MagicMock()
    inst.server_show.return_value = {"name": "vm-1", "status": "ACTIVE"}
    inst.server_stop.return_value = (True, None)
    inst.server_start.return_value = (True, None)
    cls = mocker.patch("app.tasks.OpenStackService", return_value=inst)
    return cls, inst


def _make_git_mock(mocker, repo_path):
    """Patch the ``git_service`` symbol used inside tasks.py."""
    gs = mocker.patch("app.tasks.git_service")
    gs.clone_release.return_value = repo_path
    gs.cleanup_repository.return_value = None
    return gs


def _patch_git_repo(mocker):
    """Patch the ``git.Repo(...)`` call inside the inline ``import git`` block."""
    fake_commit = mocker.MagicMock()
    fake_commit.hexsha = "abcdef1234567890"
    fake_commit.message = "commit msg"
    fake_commit.author = "Tester"
    fake_commit.committed_datetime.isoformat.return_value = "2024-01-01T00:00:00"
    repo_obj = mocker.MagicMock()
    repo_obj.head.commit = fake_commit

    import sys

    fake_git_mod = mocker.MagicMock()
    fake_git_mod.Repo.return_value = repo_obj
    mocker.patch.dict(sys.modules, {"git": fake_git_mod})
    return fake_commit


def _seed_tofu_dir(repo_path: str) -> str:
    """Create the ``tofu/`` subdir so the directory check passes."""
    tofu_dir = os.path.join(repo_path, "tofu")
    os.makedirs(tofu_dir, exist_ok=True)
    return tofu_dir


# --- Pure helper unit tests -------------------------------------------------


@pytest.mark.unit
class TestEncodeTofuVars:
    """Verify the encoding helper produces CLI-safe strings."""

    def test_dict_value_is_json_encoded(self):
        """A dict value is serialised to a JSON object literal, not str()."""
        out = encode_tofu_vars({"users": {"Team-1": [{"email": "a@b"}]}})
        # Must round-trip through JSON, not Python repr (no single quotes).
        assert json.loads(out["users"]) == {"Team-1": [{"email": "a@b"}]}

    def test_list_value_is_json_encoded(self):
        """A list value comes out as a JSON array literal."""
        out = encode_tofu_vars({"ports": [22, 80]})
        assert out["ports"] == "[22, 80]"

    def test_bool_lowercased(self):
        """HCL accepts only lowercase booleans, never Python's ``True``."""
        out = encode_tofu_vars({"flag": True, "off": False})
        assert out["flag"] == "true"
        assert out["off"] == "false"

    def test_none_top_level_dropped(self):
        """A top-level ``None`` value is omitted entirely (no -var emitted)."""
        out = encode_tofu_vars({"keep": "x", "drop": None})
        assert out == {"keep": "x"}

    def test_nested_none_scrubbed(self):
        """Nested ``None`` keys/items are stripped recursively before encoding."""
        out = encode_tofu_vars({"m": {"a": None, "b": [1, None, 2]}})
        assert json.loads(out["m"]) == {"b": [1, 2]}


@pytest.mark.unit
class TestStripFileVars:
    """File-shape vars are filtered before destroy/cleanup."""

    def test_strips_file_shape_value(self):
        """A map with content_b64 slots is recognised and removed."""
        vars_in = {
            "files": {"k": {"content_b64": "abc", "name": "a"}},
            "name": "kept",
        }
        out = _strip_file_vars(vars_in)
        assert "files" not in out
        assert out["name"] == "kept"

    def test_keeps_non_file_dict(self):
        """A regular dict-shaped variable survives the strip."""
        out = _strip_file_vars({"users": {"Team-1": ["a"]}})
        assert "users" in out

    def test_looks_like_file_var_rejects_metadata_only(self):
        """A dict with metadata but no content_b64 is NOT classified as file."""
        assert not _looks_like_file_var_value({"k": {"name": "a", "size": 1}})


@pytest.mark.unit
class TestScrubNestedNones:
    """Recursive None-scrubber walks dicts and lists."""

    def test_keeps_bool_false(self):
        """``False`` must survive scrubbing — it's a valid value, not None."""
        assert _scrub_nested_nones({"f": False}) == {"f": False}

    def test_drops_none_in_list(self):
        """``None`` entries are filtered out of lists."""
        assert _scrub_nested_nones([1, None, 2]) == [1, 2]


@pytest.mark.unit
class TestSchemaName:
    """Schema names are unquoted identifiers — hyphens become underscores."""

    def test_replaces_hyphens(self):
        """UUIDs with hyphens are converted into a single ``deployment_<safe>``."""
        out = _tfstate_schema_name("11111111-2222-3333-4444-555555555555")
        assert "-" not in out
        assert out.startswith("deployment_")


@pytest.mark.unit
class TestDeployPhases:
    """The deploy phase set is fixed — there is no image-build block any more."""

    def test_no_image_build_phases(self):
        """No phase name refers to an image build."""
        assert not any("PACKER" in p for p in _PHASES_DEPLOY)

    def test_tofu_phases_in_order(self):
        """init → plan → apply, in that order."""
        idx = [_PHASES_DEPLOY.index(p) for p in ("TOFU_INIT", "TOFU_PLAN", "TOFU_APPLY")]
        assert idx == sorted(idx)


@pytest.mark.unit
class TestPhaseTrackerUnknownPhase:
    """Unknown phases are logged but never advance the progress bar."""

    def test_mark_unknown_phase_does_not_call_progress(self, mocker):
        """An unknown phase emits a phase() log marker but no progress() event."""
        fake_logger = mocker.MagicMock()
        tracker = _PhaseTracker(fake_logger, ("A", "B"))
        tracker.mark("NOT_IN_LIST")
        fake_logger.phase.assert_called_with("NOT_IN_LIST")
        fake_logger.progress.assert_not_called()


@pytest.mark.unit
class TestFailureRoundTrip:
    """Failure exception serialises everything into args[0] for celery."""

    def test_to_dict_contains_payload(self):
        """to_dict round-trips through the JSON in ``args[0]``."""
        f = Failure("err", "dep-1", logs_dict=[{"k": "v"}], tf_state="state")
        d = f.to_dict()
        assert d["error"] == "err"
        assert d["deployment_id"] == "dep-1"
        assert d["tf_state"] == "state"

    def test_pickle_round_trip_via_reduce(self):
        """``__reduce__`` allows pickling without re-encoding the payload."""
        import pickle

        f = Failure("err", "dep-1", logs_dict=[], tf_state=None)
        restored = pickle.loads(pickle.dumps(f))
        assert isinstance(restored, Failure)
        assert restored.deployment_id == "dep-1"


@pytest.mark.unit
class TestBuildCurrentRoster:
    """Roster helper produces correct team and team-user composite keys."""

    def test_handles_dict_and_string_members(self):
        """Members may be raw email strings or dicts with an ``email`` field."""
        teams = {"T1": ["a@x"], "T2": [{"email": "b@x"}]}
        team_keys, user_keys = _build_current_roster(teams)
        assert team_keys == {"T1", "T2"}
        assert "T1-a@x" in user_keys and "T2-b@x" in user_keys


@pytest.mark.unit
class TestReconcileScopedVarsToRoster:
    """Stale scoped maps are intersected with the current roster."""

    def test_drops_orphan_slots(self, mocker):
        """A scoped var keyed by team drops keys that no longer exist."""
        fake_logger = mocker.MagicMock()
        teams = {"T1": ["a@x"]}
        vars_in = {"flavor": {"T1": "small", "T_OLD": "large"}}
        out = _reconcile_scoped_vars_to_roster(vars_in, teams, fake_logger)
        assert out["flavor"] == {"T1": "small"}

    def test_users_key_is_passed_through(self, mocker):
        """The reserved ``users`` injection is never reconciled."""
        fake_logger = mocker.MagicMock()
        teams = {"T1": ["a@x"]}
        vars_in = {"users": teams}
        out = _reconcile_scoped_vars_to_roster(vars_in, teams, fake_logger)
        assert out["users"] is teams


# --- deploy_application integration paths -----------------------------------


@pytest.mark.unit
class TestDeployApplication:
    """Branch coverage for the main deploy task body."""

    def _prepare(self, mocker, repo_path):
        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _make_openstack_service_mock(mocker)
        return _make_tofu_executor_mock(mocker)

    def _run(self, **overrides):
        kwargs = {
            "deployment_id": "dep-1",
            "app_id": "myapp",
            "app_git_link": "https://git/repo.git",
            "release": "v1",
            "user_vars": {"tofu": {"region": "eu"}},
            "teams": {"T1": [{"email": "a@x"}]},
            "openstack_envelope": {"project_id": "p1"},
        }
        kwargs.update(overrides)
        return deploy_application.run(**kwargs)

    def test_happy_path(self, mocker, tmp_path):
        """tofu init → plan → apply, outputs land in ``tofu_outputs``."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)
        cls, tofu_inst = self._prepare(mocker, repo_path)

        result = self._run()

        assert result["status"] == "success"
        assert result["deployment_id"] == "dep-1"
        assert result["tofu_outputs"] == {"ip": "1.2.3.4"}
        assert cls.call_args.args[0] == os.path.join(repo_path, "tofu")
        assert tofu_inst.init.called and tofu_inst.plan.called and tofu_inst.apply.called
        assert not tofu_inst.destroy.called
        applied_vars = tofu_inst.apply.call_args.kwargs["variables"]
        assert applied_vars["region"] == "eu"
        assert json.loads(applied_vars["users"]) == {"T1": [{"email": "a@x"}]}
        assert "image_name" not in applied_vars

    def test_packer_dir_is_rejected(self, mocker, tmp_path):
        """A repo that still ships ``packer/`` fails before any tofu call."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(os.path.join(repo_path, "packer"))
        _seed_tofu_dir(repo_path)
        _, tofu_inst = self._prepare(mocker, repo_path)

        with pytest.raises(Failure) as exc:
            self._run()

        assert "Packer is no longer supported" in str(exc.value)
        tofu_inst.init.assert_not_called()

    def test_terraform_dir_is_rejected(self, mocker, tmp_path):
        """A repo with ``terraform/`` but no ``tofu/`` names the fix."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(os.path.join(repo_path, "terraform"))
        _, tofu_inst = self._prepare(mocker, repo_path)

        with pytest.raises(Failure) as exc:
            self._run()

        assert "move terraform/ to tofu/" in str(exc.value)
        tofu_inst.init.assert_not_called()

    def test_missing_tofu_dir_raises(self, mocker, tmp_path):
        """No ``tofu/`` and nothing legacy either → plain not-found error."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _, tofu_inst = self._prepare(mocker, repo_path)

        with pytest.raises(Failure) as exc:
            self._run()

        assert "OpenTofu directory not found" in str(exc.value)
        tofu_inst.init.assert_not_called()

    def test_tofu_apply_failure_triggers_cleanup_destroy(self, mocker, tmp_path):
        """A non-zero ``tofu apply`` raises Failure and attempts cleanup destroy."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _make_openstack_service_mock(mocker)
        _, tofu_inst = _make_tofu_executor_mock(mocker, apply_=False)

        with pytest.raises(Failure) as exc:
            deploy_application.run(
                deployment_id="dep-2",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"tofu": {}},
                teams=None,
                openstack_envelope={"project_id": "p1"},
            )

        assert "OpenTofu apply failed" in str(exc.value)
        assert tofu_inst.destroy.called  # cleanup-after-failure
        assert tofu_inst.apply.called

    def test_missing_envelope_raises_failure(self, mocker, tmp_path):
        """No ``openstack_envelope`` short-circuits with a Failure exception."""
        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, str(tmp_path / "repo"))
        with pytest.raises(Failure) as exc:
            deploy_application.run(
                deployment_id="dep-x",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"tofu": {}},
                teams=None,
                openstack_envelope=None,
            )
        assert "envelope" in str(exc.value).lower()

    def test_no_plaintext_secret_in_logs(self, mocker, tmp_path):
        """No secret value from openstack_envelope leaks into the captured logs."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _make_openstack_service_mock(mocker)
        _make_tofu_executor_mock(mocker)

        secret = "SUPER-SECRET-PASSWORD-9999"
        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"tofu": {}},
            teams=None,
            openstack_envelope={
                "project_id": "p1",
                "password": secret,  # opaque encrypted blob in real life
            },
        )
        serialised = json.dumps(result["logs"])
        assert secret not in serialised

    def test_variable_encoding_dict_passed_as_json(self, mocker, tmp_path):
        """A dict in user_vars["tofu"] reaches tofu as JSON string."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _make_openstack_service_mock(mocker)
        _, tofu_inst = _make_tofu_executor_mock(mocker)

        deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"tofu": {"mapping": {"k": "v"}, "list": [1, 2]}},
            teams=None,
            openstack_envelope={"project_id": "p1"},
        )
        applied_vars = tofu_inst.apply.call_args.kwargs["variables"]
        assert json.loads(applied_vars["mapping"]) == {"k": "v"}
        assert applied_vars["list"] == "[1, 2]"


# --- destroy_deployment -----------------------------------------------------


@pytest.mark.unit
class TestDestroyDeployment:
    """Branches in the destroy task body."""

    def test_happy_path_emits_success(self, mocker, tmp_path):
        """A successful destroy returns status=success and calls tofu.destroy."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _, tofu_inst = _make_tofu_executor_mock(mocker)

        result = destroy_deployment.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"tofu": {}},
            teams=None,
            openstack_envelope={"project_id": "p1"},
        )
        assert result["status"] == "success"
        assert result["tofu_outputs"] == {}
        assert tofu_inst.destroy.called

    def test_destroy_failure_raises_failure(self, mocker, tmp_path):
        """A failing ``tofu destroy`` raises Failure with destroy-failed message."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _make_tofu_executor_mock(mocker, destroy=False)

        with pytest.raises(Failure) as exc:
            destroy_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"tofu": {}},
                teams=None,
                openstack_envelope={"project_id": "p1"},
            )
        assert "OpenTofu destroy failed" in str(exc.value)


# --- pause / resume ---------------------------------------------------------


@pytest.mark.unit
class TestPauseResume:
    """Pause/resume share a body. Verify both call the right OpenStack method."""

    def _run(self, mocker, tmp_path, task, *, server_op):
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, task)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        state = json.dumps(
            {
                "resources": [
                    {
                        "type": "openstack_compute_instance_v2",
                        "instances": [
                            {"attributes": {"id": "srv-1", "name": "web"}},
                            {"attributes": {"id": "srv-2", "name": "db"}},
                        ],
                    }
                ]
            }
        )
        _make_tofu_executor_mock(mocker, state=state)
        _, os_inst = _make_openstack_service_mock(mocker)

        result = task.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={},
            teams=None,
            openstack_envelope={"project_id": "p1"},
        )
        assert result["status"] == "success"
        method = getattr(os_inst, f"server_{server_op}")
        assert method.call_count == 2

    def test_pause_calls_server_stop_per_vm(self, mocker, tmp_path):
        """Pause runs ``server_stop`` once for each server discovered in state."""
        self._run(mocker, tmp_path, pause_deployment, server_op="stop")

    def test_resume_calls_server_start_per_vm(self, mocker, tmp_path):
        """Resume runs ``server_start`` once for each server discovered in state."""
        self._run(mocker, tmp_path, resume_deployment, server_op="start")

    def test_pause_no_servers_raises_failure(self, mocker, tmp_path):
        """Empty state → no servers found → Failure (deploy never reached apply)."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, pause_deployment)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        _make_tofu_executor_mock(mocker, state='{"resources": []}')
        _, os_inst = _make_openstack_service_mock(mocker)

        with pytest.raises(Failure) as exc:
            pause_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={},
                teams=None,
                openstack_envelope={"project_id": "p1"},
            )
        assert "No compute instances" in str(exc.value)
        os_inst.server_stop.assert_not_called()

    def test_pause_partial_failure_surfaces_ids(self, mocker, tmp_path):
        """If a single server fails to stop, the error message names it."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, pause_deployment)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        state = json.dumps(
            {
                "resources": [
                    {
                        "type": "openstack_compute_instance_v2",
                        "instances": [
                            {"attributes": {"id": "ok-1"}},
                            {"attributes": {"id": "bad-1"}},
                        ],
                    }
                ]
            }
        )
        _make_tofu_executor_mock(mocker, state=state)
        os_inst = mocker.MagicMock()
        os_inst.server_show.return_value = {"name": "n", "status": "ACTIVE"}
        os_inst.server_stop.side_effect = [(True, None), (False, "locked")]
        mocker.patch("app.tasks.OpenStackService", return_value=os_inst)

        with pytest.raises(Failure) as exc:
            pause_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={},
                teams=None,
                openstack_envelope={"project_id": "p1"},
            )
        msg = str(exc.value)
        assert "bad-1" in msg
        assert "locked" in msg


# --- redeploy_resource ------------------------------------------------------


@pytest.mark.unit
class TestRedeployResource:
    """Per-VM redeploy uses ``-target`` and ``-replace``."""

    def test_apply_called_with_target_and_replace(self, mocker, tmp_path):
        """tofu.apply receives the resource address in both targets and replace."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, redeploy_resource)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _, tofu_inst = _make_tofu_executor_mock(mocker)

        addr = 'openstack_compute_instance_v2.team_ide["Team-A"]'
        result = redeploy_resource.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"tofu": {}},
            teams=None,
            openstack_envelope={"project_id": "p1"},
            resource_address=addr,
        )
        assert result["status"] == "success"
        kwargs = tofu_inst.apply.call_args.kwargs
        assert kwargs["targets"] == [addr]
        assert kwargs["replace"] == [addr]

    def test_invalid_address_short_circuits(self, mocker, tmp_path):
        """A malformed resource_address fails fast before any clone or tofu call."""
        _silence_events(mocker, redeploy_resource)
        _make_git_mock(mocker, str(tmp_path / "repo"))
        with pytest.raises(Failure) as exc:
            redeploy_resource.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"tofu": {}},
                teams=None,
                openstack_envelope={"project_id": "p1"},
                resource_address="; rm -rf /",
            )
        assert "invalid resource_address" in str(exc.value)


# --- state / output collection ----------------------------------------------


@pytest.mark.unit
class TestCollectHelpers:
    """Best-effort state/output snapshots never raise into the task body."""

    def _executor(self, mocker, **attrs):
        inst = mocker.MagicMock(**attrs)
        mocker.patch("app.tasks.TofuExecutor", return_value=inst)
        return inst

    def test_state_missing_dir_returns_none(self, mocker, tmp_path):
        """No tofu dir yet (clone failed) → nothing to pull."""
        assert collect_tofu_state_helper(str(tmp_path / "nope"), {}, None, "s", mocker.MagicMock()) is None

    def test_state_pull_error_without_fallback_returns_none(self, mocker, tmp_path):
        """A raising pull is logged and swallowed when no local fallback is allowed."""
        self._executor(mocker, **{"state_pull.side_effect": RuntimeError("pg down")})
        task_logger = mocker.MagicMock()
        assert collect_tofu_state_helper(str(tmp_path), {}, None, "s", task_logger) is None
        assert "pg down" in task_logger.warning.call_args.args[0]

    def test_state_pull_error_falls_back_to_local_file(self, mocker, tmp_path):
        """With ``local_fallback`` a failed pull reads ``terraform.tfstate`` next to the config."""
        self._executor(mocker, **{"state_pull.side_effect": RuntimeError("pg down")})
        (tmp_path / "terraform.tfstate").write_text('{"version": 4}')
        out = collect_tofu_state_helper(str(tmp_path), {}, None, "s", mocker.MagicMock(), local_fallback=True)
        assert out == '{"version": 4}'

    def test_empty_pull_without_local_file_returns_none(self, mocker, tmp_path):
        """Empty pull + fallback but no local state file → None."""
        self._executor(mocker, **{"state_pull.return_value": ""})
        out = collect_tofu_state_helper(str(tmp_path), {}, None, "s", mocker.MagicMock(), local_fallback=True)
        assert out is None

    def test_unreadable_local_file_is_logged(self, mocker, tmp_path):
        """A local state path that can't be read degrades to a warning."""
        self._executor(mocker, **{"state_pull.return_value": ""})
        (tmp_path / "terraform.tfstate").mkdir()  # a directory: open() raises
        task_logger = mocker.MagicMock()
        out = collect_tofu_state_helper(str(tmp_path), {}, None, "s", task_logger, local_fallback=True)
        assert out is None
        task_logger.warning.assert_called_once()

    def test_outputs_error_returns_none(self, mocker, tmp_path):
        """A raising ``tofu output`` is logged and yields None."""
        self._executor(mocker, **{"output.side_effect": RuntimeError("boom")})
        task_logger = mocker.MagicMock()
        assert collect_tofu_outputs_helper(str(tmp_path), {}, None, "s", task_logger) is None
        task_logger.warning.assert_called_once()


@pytest.mark.unit
class TestDestroyStaleDataSource:
    """A data source deleted out-of-band must not block the teardown."""

    def test_retries_without_refresh(self, mocker, tmp_path):
        """The 'no results' error triggers exactly one retry with ``refresh=False``."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_tofu_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        _, tofu_inst = _make_tofu_executor_mock(mocker)
        tofu_inst.destroy.side_effect = [
            (False, "Error: Your query returned no results.", ""),
            (True, "Destroy complete!", ""),
        ]

        result = destroy_deployment.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"tofu": {}},
            teams=None,
            openstack_envelope={"project_id": "p1"},
        )

        assert result["status"] == "success"
        assert tofu_inst.destroy.call_count == 2
        assert tofu_inst.destroy.call_args.kwargs["refresh"] is False
