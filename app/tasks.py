import json
import os
import re
from typing import Any

import git

from .celery_app import celery_app
from .config import settings
from .services import OpenStackService, PerTaskCloudsConfig, TofuExecutor, git_service
from .utils.logger import LogCategory, get_logger

logger = get_logger(__name__)


def _tfstate_schema_name(deployment_id: str) -> str:
    """Postgres schema name for one deployment's OpenTofu state.

    UUIDs contain hyphens, which would force every reference to be
    double-quoted. Replacing hyphens with underscores keeps the schema
    a plain unquoted identifier and avoids escaping hazards in any
    backend-config plumbing.
    """
    return f"deployment_{deployment_id.replace('-', '_')}"


class Failure(Exception):
    """Custom exception that carries deployment details for Celery.

    The full failure payload is serialised once into ``args[0]`` as a JSON
    string. The backend's celery event listener parses that JSON back via
    a ``Failure\\('<json>'\\)`` regex over the traceback.

    ``__reduce__`` is overridden so pickle reconstructs the exception via
    the ``_from_payload`` classmethod, which accepts the single JSON string
    directly.
    """

    def __init__(
        self,
        message: str,
        deployment_id: str,
        logs_dict: list[dict[str, Any]] | dict[str, Any],
        tf_state: str | None = None,
        commit_info: dict[str, Any] | None = None,
        tofu_outputs: dict[str, Any] | None = None,
    ):
        self.deployment_id = deployment_id
        self.logs_dict = logs_dict
        self.tf_state = tf_state
        self.commit_info = commit_info
        self.tofu_outputs = tofu_outputs

        # Encode all data as JSON in the exception message
        data = {
            "error": message,
            "deployment_id": deployment_id,
            "logs": logs_dict,
            "tf_state": tf_state,
            "commit_info": commit_info,
            "tofu_outputs": tofu_outputs,
        }
        super().__init__(json.dumps(data))

    @classmethod
    def _from_payload(cls, payload: str) -> "Failure":
        """Reconstruct a Failure from its serialised JSON payload.

        Used by ``__reduce__`` so pickle can round-trip the exception.
        """
        data = json.loads(payload)
        instance = cls.__new__(cls)
        instance.deployment_id = data.get("deployment_id", "")
        instance.logs_dict = data.get("logs")
        instance.tf_state = data.get("tf_state")
        instance.commit_info = data.get("commit_info")
        instance.tofu_outputs = data.get("tofu_outputs")
        Exception.__init__(instance, payload)
        return instance

    def __reduce__(self):
        # The single-arg constructor here is ``_from_payload``; args[0] is
        # the JSON string we built in __init__.
        return (Failure._from_payload, (self.args[0] if self.args else "{}",))

    def __repr__(self) -> str:
        # Pin the repr format that the backend's celery event listener
        # relies on (regex ``Failure\('(.+)'\)``).
        return f"Failure({self.args[0]!r})" if self.args else "Failure()"

    def to_dict(self) -> dict[str, Any]:
        """Convert exception data to dict for serialization"""
        return json.loads(str(self))


# --- Variable encoding for the OpenTofu CLI --------------------------------


def _looks_like_file_var_value(value: Any) -> bool:
    """True if ``value`` matches the file-upload shape produced by
    the backend's ``_attach_files_to_user_input``: a non-empty
    mapping whose entries each carry a ``content_b64`` field plus
    the metadata triplet (name, size, content_type) — i.e. exactly
    the ``map(object(...))`` HCL contract.

    Used by :func:`_strip_file_vars` so destroy / cleanup-after-
    failure can drop ``@openstack:file:*``-marked variables before
    passing the var-set to ``tofu destroy``. OpenTofu
    validates *all* declared variables on every command — including
    destroy — so an apply-only file-var would otherwise block the
    cleanup with a schema error.

    A slot must carry ``content_b64`` to qualify as a file-var; the
    strictness avoids dropping legitimate non-file map variables that
    happen to share the metadata keys.
    """
    if not isinstance(value, dict) or not value:
        return False
    for slot in value.values():
        if not isinstance(slot, dict):
            return False
        if "content_b64" not in slot:
            return False
    return True


def _strip_file_vars(tofu_vars: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``tofu_vars`` with file-shape entries removed.

    Pure function — never mutates the input. Used by destroy and the
    deploy cleanup-after-failure branches; deploy itself keeps the
    file vars because ``apply`` consumes them via cloud-init.
    """
    return {k: v for k, v in tofu_vars.items() if not _looks_like_file_var_value(v)}


def _scrub_nested_nones(value: Any) -> Any:
    """Recursively drop ``None`` entries from nested dicts/lists.

    A stray ``None`` inside a ``map(list(string))`` slot would surface as
    literal HCL ``null`` after the JSON round-trip and trip OpenTofu's
    type check. Dicts have their ``None``-valued keys removed, lists have
    their ``None`` entries filtered out, and both are walked recursively.
    Scalars (including bools) pass through untouched.
    """
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = _scrub_nested_nones(v)
        return cleaned
    if isinstance(value, list):
        return [_scrub_nested_nones(item) for item in value if item is not None]
    return value


def encode_tofu_vars(d: dict[str, Any]) -> dict[str, str]:
    """Encode variables for ``tofu -var key=value`` CLI args.

    OpenTofu reads complex types (objects, tuples) when the value is a
    valid JSON literal. We JSON-encode dicts/lists once and pass them
    through verbatim — no string normalisation that could damage escape
    sequences.

    Nested ``None`` values are scrubbed recursively (see
    :func:`_scrub_nested_nones`) so a stray ``null`` deep inside a
    ``map(list(string))`` slot can't trip OpenTofu's type check.
    """
    result: dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            # HCL accepts lowercase only; ``str(True)`` would emit "True".
            result[k] = "true" if v else "false"
        elif isinstance(v, dict | list):
            result[k] = json.dumps(_scrub_nested_nones(v), ensure_ascii=False)
        else:
            result[k] = str(v)
    return result


# --- Phase tracking ----------------------------------------------------------
#
# Phases are pinned by name (a string the frontend renders as a stepper) and
# by index (1-based, used for the percent bar).

PHASE_STARTING = "STARTING"
PHASE_OPENSTACK_SETUP = "OPENSTACK_SETUP"
PHASE_GIT_CLONE = "GIT_CLONE"
PHASE_CREDS_MATERIALISE = "CREDS_MATERIALISE"
PHASE_TOFU_INIT = "TOFU_INIT"
PHASE_TOFU_PLAN = "TOFU_PLAN"
PHASE_TOFU_APPLY = "TOFU_APPLY"
PHASE_OUTPUTS_AND_CLEANUP = "OUTPUTS_AND_CLEANUP"
PHASE_TOFU_DESTROY = "TOFU_DESTROY"
PHASE_CLEANUP = "CLEANUP"
# Pause/resume share the deploy/destroy preamble but their hot phase is
# a CLI-driven server stop/start, so they get distinct phase names rather
# than reusing TOFU_DESTROY.
PHASE_SERVER_STOP = "SERVER_STOP"
PHASE_SERVER_START = "SERVER_START"

_PHASES_DEPLOY = (
    PHASE_STARTING,
    PHASE_OPENSTACK_SETUP,
    PHASE_GIT_CLONE,
    PHASE_CREDS_MATERIALISE,
    PHASE_TOFU_INIT,
    PHASE_TOFU_PLAN,
    PHASE_TOFU_APPLY,
    PHASE_OUTPUTS_AND_CLEANUP,
)

# Destroy uses a shorter pipeline — no plan (tofu destroy has its own
# planning step internally that we don't surface as its own progress
# phase).
_PHASES_DESTROY = (
    PHASE_STARTING,
    PHASE_OPENSTACK_SETUP,
    PHASE_GIT_CLONE,
    PHASE_CREDS_MATERIALISE,
    PHASE_TOFU_INIT,
    PHASE_TOFU_DESTROY,
    PHASE_CLEANUP,
)
# Per-VM redeploy reuses the destroy preamble (git clone at the same
# release tag, materialise clouds.yaml, tofu init) and then runs
# ``tofu apply -replace=<addr> -target=<addr>`` instead of
# ``destroy``. The shape mirrors destroy exactly so the SSE phase bar
# in the UI stays familiar.
_PHASES_REDEPLOY = (
    PHASE_STARTING,
    PHASE_OPENSTACK_SETUP,
    PHASE_GIT_CLONE,
    PHASE_CREDS_MATERIALISE,
    PHASE_TOFU_INIT,
    PHASE_TOFU_APPLY,
    PHASE_CLEANUP,
)
# Pause / resume share the destroy preamble — git clone at the same
# release tag, materialise clouds.yaml, tofu init so we can pull
# the canonical state from the pg backend. The hot phase is the
# server stop / start loop. CLEANUP runs the repo shred and (for the
# log) a final state pull, mirroring destroy's tail.
_PHASES_PAUSE = (
    PHASE_STARTING,
    PHASE_OPENSTACK_SETUP,
    PHASE_GIT_CLONE,
    PHASE_CREDS_MATERIALISE,
    PHASE_TOFU_INIT,
    PHASE_SERVER_STOP,
    PHASE_CLEANUP,
)
_PHASES_RESUME = (
    PHASE_STARTING,
    PHASE_OPENSTACK_SETUP,
    PHASE_GIT_CLONE,
    PHASE_CREDS_MATERIALISE,
    PHASE_TOFU_INIT,
    PHASE_SERVER_START,
    PHASE_CLEANUP,
)


class _PhaseTracker:
    """Drives ``StructuredLogger.progress`` calls.

    The set of phases is fixed at construction time so the percent bar
    monotonically advances; ``mark()`` looks up the index of the named
    phase and sends a progress event with the correct ``idx/total``.
    """

    def __init__(self, logger: Any, phases: tuple[str, ...]):
        self._logger = logger
        self._phases = phases
        self._index_by_name = {name: i for i, name in enumerate(phases, start=1)}

    @property
    def total(self) -> int:
        return len(self._phases)

    def mark(self, phase_name: str, message: str = "") -> None:
        idx = self._index_by_name.get(phase_name)
        if idx is None:
            # Unknown phase — emit a transcript marker but no progress
            # update so the bar doesn't reset.
            self._logger.phase(phase_name)
            return
        # Buffer the readable phase header and emit the live progress event.
        # The full phase-name sequence rides on every event so the UI can
        # render each stepper slot with its real label immediately.
        self._logger.phase(phase_name)
        self._logger.progress(
            phase_name,
            idx,
            self.total,
            message,
            phase_names=self._phases,
        )


def _tofu_executor(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema) -> TofuExecutor:
    """Build a TofuExecutor bound to the deployment's pg-backend schema."""
    return TofuExecutor(
        tofu_dir,
        env_vars=openstack_env,
        backend_conn_str=tfstate_conn_str,
        backend_schema_name=tfstate_schema,
    )


def collect_tofu_state_helper(
    tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger, *, local_fallback=False
):
    """Snapshot the tofu state for the task row (best-effort).

    With the pg backend the canonical state lives in Postgres; this
    snapshot is used for debugging only. When ``local_fallback`` is set
    (deploy path), a missing/empty pull falls back to reading the local
    ``terraform.tfstate`` file for legacy/test modes that don't configure
    a remote backend. Returns ``None`` when nothing could be read.
    """
    if not (tofu_dir and os.path.exists(tofu_dir)):
        return None
    try:
        pulled = _tofu_executor(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema).state_pull()
        if pulled or not local_fallback:
            return pulled
    except Exception as e:
        task_logger.warning(f"Could not pull tofu state: {e}", category=LogCategory.WARNING)
        if not local_fallback:
            return None

    # Legacy fallback — only relevant when no pg backend is configured.
    tfstate_path = os.path.join(tofu_dir, "terraform.tfstate")
    if os.path.exists(tfstate_path):
        try:
            with open(tfstate_path) as f:
                return f.read()
        except Exception as e:
            task_logger.warning(f"Could not read tofu state: {e}", category=LogCategory.WARNING)
    return None


def collect_tofu_outputs_helper(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger):
    """Collect tofu outputs even on partial success. ``None`` on failure."""
    if tofu_dir and os.path.exists(tofu_dir):
        try:
            return _tofu_executor(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema).output()
        except Exception as e:
            task_logger.warning(f"Could not read tofu outputs: {e}", category=LogCategory.WARNING)
    return None


def _extract_commit_info(repo_path: str) -> dict[str, Any]:
    """Read the checked-out commit's metadata from a cloned repo.

    Returns the dict shape persisted into the task result and the
    ``Failure`` payload. Callers wrap this in their own try/except so a
    repo without a readable HEAD degrades to a warning, not a hard fail.
    """
    repo = git.Repo(repo_path)
    commit = repo.head.commit
    return {
        "hash": commit.hexsha,
        "message": commit.message.strip(),
        "author": str(commit.author),
        "date": commit.committed_datetime.isoformat(),
    }


def _resolve_tofu_dir(repo_path: str) -> str:
    """Return the cloned repo's ``tofu/`` directory or raise a clear error.

    The app contract is OpenTofu-only: one ``tofu/`` directory, no image
    build. A repo that still ships ``packer/`` or ``terraform/`` was written
    for the old contract and is rejected outright instead of being half-run,
    so the app developer sees what to change rather than a confusing
    failure further down the pipeline.
    """
    if os.path.isdir(os.path.join(repo_path, "packer")):
        raise Exception(
            "Packer is no longer supported: remove the packer/ directory and "
            "provision the VM via cloud-init user_data in tofu/ instead"
        )
    tofu_dir = os.path.join(repo_path, "tofu")
    if os.path.isdir(tofu_dir):
        return tofu_dir
    if os.path.isdir(os.path.join(repo_path, "terraform")):
        raise Exception("Terraform is no longer supported: move terraform/ to tofu/ and rename *.tf to *.tofu")
    raise Exception(f"OpenTofu directory not found at {tofu_dir}")


def _cleanup_task_resources(clouds_config: PerTaskCloudsConfig | None, repo_path: str | None, task_logger: Any) -> None:
    """Best-effort teardown shared by every task's ``finally`` block.

    Shreds the per-task clouds.yaml first (so the credential file is gone
    even if the repo cleanup below fails or hangs), then removes the
    cloned repo. Both steps swallow their own errors as warnings so the
    task's real result/exception is never masked by cleanup noise.
    """
    if clouds_config is not None:
        try:
            clouds_config.__exit__(None, None, None)
        except Exception as e:
            task_logger.warning(
                f"Per-task clouds.yaml cleanup failed: {e}",
                category=LogCategory.WARNING,
            )
    if repo_path:
        try:
            git_service.cleanup_repository(repo_path)
            task_logger.success("Repository cleanup completed", category=LogCategory.SYSTEM)
        except Exception as e:
            task_logger.warning(f"Repository cleanup failed: {e}", category=LogCategory.WARNING)


@celery_app.task(bind=True, name="tasks.deploy_application")
def deploy_application(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] = None,
    openstack_envelope: dict[str, Any] | None = None,
):
    """
    Deploy an application using OpenTofu

    Args:
        deployment_id: UUID of the deployment
        app_git_link: Git repo URL
        release: Tag/Release to checkout
        user_vars: User variables for OpenTofu
        teams: Teams with user emails {"team_name": [{"email": "user@example.com"}]}
        openstack_envelope: Encrypted per-user OpenStack credential envelope
            shipped from the backend. Required for new deploys; the optional
            default exists only so older queued messages don't crash the
            worker on rollout (we raise immediately if it's missing).

    Returns:
        dict: status, logs, tf_state, commit_info, tofu_outputs
    """
    task_logger = get_logger(f"deploy:{deployment_id}", correlation_id=deployment_id)

    # Wire the per-deployment logger to Celery's event bus. Every buffered
    # log entry now becomes a ``task-log`` event, and ``task_logger.progress``
    # emits ``task-progress``. The backend's listener picks both up and
    # forwards them via the in-process pubsub to any open SSE subscriber.
    bound_task = self

    def _emit(event_name: str, payload: dict[str, Any]) -> None:
        # ``deployment_id`` is duplicated into every event so the backend
        # listener doesn't need a DB lookup to figure out which deployment
        # the event belongs to.
        bound_task.send_event(event_name, deployment_id=deployment_id, **payload)

    task_logger.set_event_emitter(_emit)

    phase_tracker = _PhaseTracker(task_logger, _PHASES_DEPLOY)

    repo_path = None
    tf_state = None
    outputs = None
    commit_info = None
    tofu_dir = None
    openstack_env: dict[str, str] = {}
    clouds_config: PerTaskCloudsConfig | None = None

    # OpenTofu's pg backend lives in a worker-only Postgres. Configured
    # at deploy/destroy time by writing a `pg_backend_override.tofu` next
    # to the cloned repo's tofu/ directory. One schema per
    # deployment isolates state and locks.
    tfstate_conn_str = settings.TFSTATE_DATABASE_URL or None
    tfstate_schema = _tfstate_schema_name(deployment_id)

    # Default teams to empty dict if not provided
    if teams is None:
        teams = {}

    def collect_tofu_state():
        return collect_tofu_state_helper(
            tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger, local_fallback=True
        )

    def collect_tofu_outputs():
        return collect_tofu_outputs_helper(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger)

    try:
        phase_tracker.mark(PHASE_STARTING, "Starting deployment")
        task_logger.resource_info(
            "deployment",
            deployment_id,
            app_id=app_id,
            git_url=app_git_link,
            release=release,
            user_vars_keys=list(user_vars.keys()),
            teams_keys=list(teams.keys()),
        )

        # Phase 1: OpenStack credentials (envelope only — materialised after
        # the git clone so the per-task clouds.yaml lives inside repo_path).
        phase_tracker.mark(PHASE_OPENSTACK_SETUP, "Validating OpenStack credentials")
        if not openstack_envelope:
            raise Exception("OpenStack credential envelope missing — user must upload credentials before deploying")
        task_logger.success(
            "OpenStack credential envelope received",
            category=LogCategory.STATUS,
        )

        # Phase 2: Git clone
        phase_tracker.mark(PHASE_GIT_CLONE, "Cloning repository")
        task_logger.info(f"Cloning repository: {app_git_link}", category=LogCategory.OPERATION)
        try:
            repo_path = git_service.clone_release(git_url=app_git_link, deployment_id=deployment_id, tag=release)

            # Get commit info
            try:
                commit_info = _extract_commit_info(repo_path)
                task_logger.resource_info(
                    "git_commit",
                    commit_info["hash"][:8],
                    hash=commit_info["hash"],
                    message=commit_info["message"],
                    author=commit_info["author"],
                )
                task_logger.success(
                    f"Repository cloned at commit {commit_info['hash'][:8]}", category=LogCategory.STATUS
                )
            except Exception as e:
                task_logger.warning(f"Could not extract commit info: {e}", category=LogCategory.WARNING)

        except Exception as e:
            raise Exception(f"Git clone failed: {str(e)}")

        # Materialise the per-task clouds.yaml inside repo_path with mode 0600.
        # Lives only for the duration of this task; shredded by __exit__.
        phase_tracker.mark(PHASE_CREDS_MATERIALISE, "Writing per-task clouds.yaml")
        task_logger.operation_start("openstack_credentials_materialise")
        clouds_config = PerTaskCloudsConfig(openstack_envelope, work_dir=repo_path)
        openstack_env = clouds_config.__enter__()
        task_logger.operation_end("openstack_credentials_materialise", success=True)
        task_logger.success(
            "Per-task clouds.yaml written",
            category=LogCategory.STATUS,
        )

        # The output callback feeds each line of subprocess output into the
        # task logger as a streaming entry, which then ships it via the
        # event emitter as a ``task-log`` event.
        def _stream_line(tool: str, line: str) -> None:
            task_logger.tool_output_line(tool, line)

        # Phase 3: OpenTofu
        tofu_dir = _resolve_tofu_dir(repo_path)

        tofu = None
        tofu_vars: dict[str, Any] = {}
        try:
            tofu = TofuExecutor(
                tofu_dir,
                env_vars=openstack_env,
                backend_conn_str=tfstate_conn_str,
                backend_schema_name=tfstate_schema,
                output_callback=_stream_line,
            )

            phase_tracker.mark(PHASE_TOFU_INIT, "Initializing OpenTofu")
            success, stdout, stderr = tofu.init()
            if not success:
                # Surface the real reason in the per-deployment log; the
                # module-level logger only writes to worker stdout, which the
                # frontend never sees.
                if stdout:
                    task_logger.command_output("tofu_init_stdout", stdout, returncode=1)
                if stderr:
                    task_logger.command_output("tofu_init_stderr", stderr, returncode=1)
                task_logger.error("OpenTofu init failed", category=LogCategory.ERROR)
                raise Exception("OpenTofu init failed")
            task_logger.success("OpenTofu initialization completed", category=LogCategory.STATUS)

            # Merge user_vars with teams for OpenTofu. Nested structures
            # pass through encode_tofu_vars unchanged.
            tofu_vars = {**user_vars["tofu"]} if "tofu" in user_vars else {}
            if teams:
                tofu_vars["users"] = teams
            tofu_vars = encode_tofu_vars(tofu_vars)

            # File-upload variables can balloon a single -var to hundreds
            # of KB. The Nova metadata service caps cloud-init user_data at
            # ~64 KB compressed, so warn per-variable above 120 KB to land
            # the heads-up in the worker log before a boot failure.
            _log_bytes_per_var_warn = 120 * 1024
            for _vname, _vstr in tofu_vars.items():
                if isinstance(_vstr, str) and len(_vstr) > _log_bytes_per_var_warn:
                    task_logger.warning(
                        f"OpenTofu variable '{_vname}' is "
                        f"{len(_vstr) // 1024} KB encoded — close to the "
                        "cloud-init user_data limit; the VM may fail to "
                        "boot if the template inlines the full value.",
                        category=LogCategory.WARNING,
                    )

            task_logger.info(
                "OpenTofu variable keys",
                category=LogCategory.OPERATION,
                keys=list(tofu_vars.keys()),
            )

            phase_tracker.mark(PHASE_TOFU_PLAN, "Planning OpenTofu deployment")
            success, stdout, stderr = tofu.plan(variables=tofu_vars)
            if not success:
                if stdout:
                    task_logger.command_output("tofu_plan_stdout", stdout, returncode=1)
                if stderr:
                    task_logger.command_output("tofu_plan_stderr", stderr, returncode=1)
                task_logger.error("OpenTofu plan failed", category=LogCategory.ERROR)
                raise Exception("OpenTofu plan failed")
            task_logger.success("OpenTofu plan completed successfully", category=LogCategory.STATUS)

            phase_tracker.mark(PHASE_TOFU_APPLY, "Applying configuration (this may take minutes)")
            success, stdout, stderr = tofu.apply(variables=tofu_vars)
            if not success:
                if stdout:
                    task_logger.command_output("tofu_apply_stdout", stdout, returncode=1)
                if stderr:
                    task_logger.command_output("tofu_apply_stderr", stderr, returncode=1)
                task_logger.error("OpenTofu apply failed", category=LogCategory.ERROR)
                raise Exception("OpenTofu apply failed")
            task_logger.success("OpenTofu resources created", category=LogCategory.STATUS)

            # Collect outputs and state
            phase_tracker.mark(PHASE_OUTPUTS_AND_CLEANUP, "Collecting outputs")
            outputs = collect_tofu_outputs()
            tf_state = collect_tofu_state()

            if outputs:
                task_logger.info(
                    "OpenTofu deployment outputs collected", category=LogCategory.OPERATION, output_count=len(outputs)
                )

        except Exception as e:
            # Try to collect partial results even on failure
            tf_state = collect_tofu_state()
            outputs = collect_tofu_outputs()

            # Best-effort cleanup: a half-finished `tofu apply` typically
            # leaves orphaned OpenStack resources (networks, ports, volumes)
            # that quietly eat the project's quota. Run destroy with the same
            # variables so the apply graph can be reversed; ignore failures
            # here — we're already in the error path and re-raising below.
            if tofu is not None and tofu_dir and os.path.exists(tofu_dir):
                try:
                    task_logger.info(
                        "Running tofu destroy to clean up partially-applied resources",
                        category=LogCategory.OPERATION,
                    )
                    # Rebuild the var-set from the raw user_vars without
                    # the file payloads. Destroy doesn't need the
                    # cloud-init bytes, but OpenTofu validates every
                    # declared var on every run, so an apply-only file-var
                    # would otherwise reject the cleanup with a schema error.
                    cleanup_tf_vars = _strip_file_vars(user_vars.get("tofu") or {})
                    if teams:
                        cleanup_tf_vars["users"] = teams
                    tofu.destroy(variables=encode_tofu_vars(cleanup_tf_vars))
                    # Refresh state after destroy so the persisted record reflects cleanup.
                    tf_state = collect_tofu_state()
                except Exception as cleanup_error:
                    task_logger.warning(
                        f"OpenTofu cleanup failed: {cleanup_error}",
                        category=LogCategory.WARNING,
                    )

            raise Exception(f"OpenTofu error: {str(e)}")

        # The OUTPUTS_AND_CLEANUP progress event was already emitted above;
        # a second mark would land on the same index, so just log success.
        task_logger.success(f"Deployment {deployment_id} completed successfully", category=LogCategory.STATUS)

        # Log summary
        summary = task_logger.get_summary()
        task_logger.info("Deployment summary", category=LogCategory.SYSTEM, **summary)

        if outputs:
            task_logger.info("OpenTofu deployment output", category=LogCategory.SYSTEM, **outputs)

        result = {
            "status": "success",
            "deployment_id": deployment_id,
            "logs": task_logger.get_logs_dict(),
            "tf_state": tf_state,
            "commit_info": commit_info,
            "tofu_outputs": outputs,
        }

        # Return result (sent via task-succeeded event)
        return result

    except Exception as e:
        task_logger.exception(f"Deployment failed: {str(e)}", exception=e, deployment_id=deployment_id)

        # Try to collect any available state/outputs even on failure
        if not tf_state:
            tf_state = collect_tofu_state()
        if not outputs:
            outputs = collect_tofu_outputs()

        # Raise custom exception with all details
        raise Failure(
            message=str(e),
            deployment_id=deployment_id,
            logs_dict=task_logger.get_logs_dict(),
            tf_state=tf_state,
            commit_info=commit_info,
            tofu_outputs=outputs,
        )

    finally:
        # Shred the per-task clouds.yaml first so the credential file is gone
        # even if the repository cleanup below fails or hangs.
        _cleanup_task_resources(clouds_config, repo_path, task_logger)


@celery_app.task(bind=True, name="tasks.destroy_deployment")
def destroy_deployment(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] = None,
    openstack_envelope: dict[str, Any] | None = None,
):
    """Tear down a deployment via ``tofu destroy``.

    Mirrors ``deploy_application``'s setup (git clone at the same release
    tag, materialise the per-task clouds.yaml, configure the same pg
    backend schema) so OpenTofu sees the exact same state it built.
    Then runs ``tofu destroy -auto-approve`` instead of
    ``plan + apply``.

    All progress and log events flow through the same ``StructuredLogger``
    + Celery custom-event pipeline as deploy, so the frontend's live
    SSE stream renders the destroy run identically to a deploy.

    Args mirror ``deploy_application`` so the backend can re-dispatch
    the same persisted values without translation.
    """
    task_logger = get_logger(f"destroy:{deployment_id}", correlation_id=deployment_id)

    bound_task = self

    def _emit(event_name: str, payload: dict[str, Any]) -> None:
        bound_task.send_event(event_name, deployment_id=deployment_id, **payload)

    task_logger.set_event_emitter(_emit)
    phase_tracker = _PhaseTracker(task_logger, _PHASES_DESTROY)

    repo_path = None
    tf_state: str | None = None
    commit_info: dict[str, Any] | None = None
    tofu_dir: str | None = None
    openstack_env: dict[str, str] = {}
    clouds_config: PerTaskCloudsConfig | None = None

    tfstate_conn_str = settings.TFSTATE_DATABASE_URL or None
    tfstate_schema = _tfstate_schema_name(deployment_id)

    if teams is None:
        teams = {}

    def _stream_line(tool: str, line: str) -> None:
        task_logger.tool_output_line(tool, line)

    def collect_tofu_state():
        return collect_tofu_state_helper(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger)

    try:
        phase_tracker.mark(PHASE_STARTING, "Starting destroy")
        task_logger.resource_info(
            "deployment",
            deployment_id,
            app_id=app_id,
            git_url=app_git_link,
            release=release,
            user_vars_keys=list(user_vars.keys()),
            teams_keys=list(teams.keys()),
            action="destroy",
        )

        phase_tracker.mark(PHASE_OPENSTACK_SETUP, "Validating OpenStack credentials")
        if not openstack_envelope:
            raise Exception("OpenStack credential envelope missing — cannot destroy without credentials")
        task_logger.success("OpenStack credential envelope received", category=LogCategory.STATUS)

        phase_tracker.mark(PHASE_GIT_CLONE, "Cloning repository at original release tag")
        task_logger.info(
            f"Cloning {app_git_link} at {release} (same ref as the original deploy "
            "so tofu code matches the pg-backend state)",
            category=LogCategory.OPERATION,
        )
        try:
            repo_path = git_service.clone_release(git_url=app_git_link, deployment_id=deployment_id, tag=release)
            try:
                commit_info = _extract_commit_info(repo_path)
                task_logger.resource_info(
                    "git_commit",
                    commit_info["hash"][:8],
                    hash=commit_info["hash"],
                    message=commit_info["message"],
                    author=commit_info["author"],
                )
                task_logger.success(
                    f"Repository cloned at commit {commit_info['hash'][:8]}", category=LogCategory.STATUS
                )
            except Exception as e:
                task_logger.warning(f"Could not extract commit info: {e}", category=LogCategory.WARNING)
        except Exception as e:
            raise Exception(f"Git clone failed: {str(e)}")

        phase_tracker.mark(PHASE_CREDS_MATERIALISE, "Writing per-task clouds.yaml")
        clouds_config = PerTaskCloudsConfig(openstack_envelope, work_dir=repo_path)
        openstack_env = clouds_config.__enter__()
        task_logger.success("Per-task clouds.yaml written", category=LogCategory.STATUS)

        tofu_dir = _resolve_tofu_dir(repo_path)

        # Drop ``@openstack:file:*`` variable values before passing
        # the var-set to tofu destroy. Files are only consumed
        # at apply-time (cloud-init write_files); destroy doesn't
        # need them, but OpenTofu validates every declared var on
        # every run.
        tofu_vars = {**user_vars["tofu"]} if "tofu" in user_vars else {}
        tofu_vars = _strip_file_vars(tofu_vars)
        if teams:
            tofu_vars["users"] = teams
        tofu_vars = encode_tofu_vars(tofu_vars)

        tofu = TofuExecutor(
            tofu_dir,
            env_vars=openstack_env,
            backend_conn_str=tfstate_conn_str,
            backend_schema_name=tfstate_schema,
            output_callback=_stream_line,
        )

        phase_tracker.mark(PHASE_TOFU_INIT, "Initializing OpenTofu")
        success, stdout, stderr = tofu.init()
        if not success:
            if stdout:
                task_logger.command_output("tofu_init_stdout", stdout, returncode=1)
            if stderr:
                task_logger.command_output("tofu_init_stderr", stderr, returncode=1)
            raise Exception("OpenTofu init failed")
        task_logger.success("OpenTofu initialization completed", category=LogCategory.STATUS)

        phase_tracker.mark(PHASE_TOFU_DESTROY, "Destroying resources")
        success, stdout, stderr = tofu.destroy(variables=tofu_vars)
        # A data source (e.g. the Glance image lookup) is re-read on every
        # destroy refresh. If that image/network was deleted out-of-band,
        # the refresh fails with "Your query returned no results" before any
        # managed resource is touched. Retry once with -refresh=false so the
        # teardown proceeds purely from state. Scoped to this exact error so
        # genuine destroy failures still surface.
        if not success and "Your query returned no results" in f"{stdout or ''}{stderr or ''}":
            task_logger.warning(
                "Destroy blocked by a stale data source (image/network deleted "
                "out-of-band). Retrying with -refresh=false — resources are torn "
                "down from state.",
                category=LogCategory.WARNING,
            )
            success, stdout, stderr = tofu.destroy(variables=tofu_vars, refresh=False)
        if not success:
            if stdout:
                task_logger.command_output("tofu_destroy_stdout", stdout, returncode=1)
            if stderr:
                task_logger.command_output("tofu_destroy_stderr", stderr, returncode=1)
            raise Exception("OpenTofu destroy failed")
        task_logger.success("OpenTofu resources destroyed", category=LogCategory.STATUS)

        phase_tracker.mark(PHASE_CLEANUP, "Pulling final state")
        tf_state = collect_tofu_state()

        task_logger.success(f"Deployment {deployment_id} destroyed successfully", category=LogCategory.STATUS)

        summary = task_logger.get_summary()
        task_logger.info("Destroy summary", category=LogCategory.SYSTEM, **summary)

        return {
            "status": "success",
            "deployment_id": deployment_id,
            "logs": task_logger.get_logs_dict(),
            "tf_state": tf_state,
            "commit_info": commit_info,
            # No outputs — destroy doesn't produce any. Field is kept for
            # event-listener parity with deploy_application's payload.
            "tofu_outputs": {},
        }

    except Exception as e:
        task_logger.exception(f"Destroy failed: {str(e)}", exception=e, deployment_id=deployment_id)
        if not tf_state:
            tf_state = collect_tofu_state()
        raise Failure(
            message=str(e),
            deployment_id=deployment_id,
            logs_dict=task_logger.get_logs_dict(),
            tf_state=tf_state,
            commit_info=commit_info,
            tofu_outputs={},
        )

    finally:
        _cleanup_task_resources(clouds_config, repo_path, task_logger)


# ----------------------------------------------------------------
# PAUSE / RESUME — compute-instance-only lifecycle
# ----------------------------------------------------------------
#
# Both tasks share the destroy preamble (git clone at the same release
# tag → per-task clouds.yaml → tofu init pointed at the pg backend)
# so we can pull the canonical tofu state and read back which
# compute instances belong to this deployment. The hot phase is a
# CLI-driven stop/start loop; tofu state is left untouched. Server
# discovery goes through the state (not tags) so no app template needs
# to opt in, and CLI idempotency lets the loop re-run safely on retry.


def _extract_compute_instance_ids(state_json: str | None) -> list[str]:
    """Return server IDs from a tofu pg-backend state dump.

    OpenTofu's serialised state shape is
    ``{"resources": [{"type": "...", "instances": [{"attributes": {"id": "..."}}]}]}``.
    Filtered to ``openstack_compute_instance_v2`` so we only stop/start
    Nova servers, not volumes / networks / security groups.

    Returns an empty list on any parsing trouble — the caller can then
    decide whether "no servers found" is a hard error (deploy never
    actually ran) or a no-op success (everything already torn down).
    """
    if not state_json:
        return []
    try:
        state = json.loads(state_json) if isinstance(state_json, str) else state_json
    except (TypeError, json.JSONDecodeError):
        return []

    ids: list[str] = []
    for resource in state.get("resources", []):
        if resource.get("type") != "openstack_compute_instance_v2":
            continue
        for instance in resource.get("instances", []):
            attrs = instance.get("attributes") or {}
            sid = attrs.get("id")
            if sid:
                ids.append(sid)
    return ids


def _run_compute_lifecycle(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] | None,
    openstack_envelope: dict[str, Any] | None,
    *,
    action: str,  # "pause" | "resume" — only for log/error labels
    phases: tuple[str, ...],
    server_phase: str,
    server_op: str,  # "stop" | "start"
):
    """Shared body for ``pause_deployment`` / ``resume_deployment``.

    Mirrors :func:`destroy_deployment`'s preamble exactly so the two
    paths stay easy to reason about. Diverges only at the hot phase:
    instead of ``tofu destroy``, we pull the state, extract
    every compute instance's ID, and shell out to
    ``openstack server stop|start`` for each.

    Failures during the per-server loop are accumulated and re-raised
    once with a list of which servers failed — so the user sees
    "stopped 4/5; failed: web-1: locked task" instead of just "pause
    failed" without any pointer to which instance is stuck.
    """
    label = f"{action}:{deployment_id}"
    task_logger = get_logger(label, correlation_id=deployment_id)

    bound_task = self

    def _emit(event_name: str, payload: dict[str, Any]) -> None:
        bound_task.send_event(event_name, deployment_id=deployment_id, **payload)

    task_logger.set_event_emitter(_emit)
    phase_tracker = _PhaseTracker(task_logger, phases)

    repo_path = None
    tofu_dir: str | None = None
    openstack_env: dict[str, str] = {}
    clouds_config: PerTaskCloudsConfig | None = None

    tfstate_conn_str = settings.TFSTATE_DATABASE_URL or None
    tfstate_schema = _tfstate_schema_name(deployment_id)

    if teams is None:
        teams = {}

    def _stream_line(tool: str, line: str) -> None:
        task_logger.tool_output_line(tool, line)

    try:
        phase_tracker.mark(PHASE_STARTING, f"Starting {action}")
        task_logger.resource_info(
            "deployment",
            deployment_id,
            app_id=app_id,
            git_url=app_git_link,
            release=release,
            action=action,
        )

        phase_tracker.mark(PHASE_OPENSTACK_SETUP, "Validating OpenStack credentials")
        if not openstack_envelope:
            raise Exception(f"OpenStack credential envelope missing — cannot {action} without credentials")
        task_logger.success("OpenStack credential envelope received", category=LogCategory.STATUS)

        phase_tracker.mark(PHASE_GIT_CLONE, "Cloning repository at original release tag")
        try:
            repo_path = git_service.clone_release(
                git_url=app_git_link,
                deployment_id=deployment_id,
                tag=release,
            )
            task_logger.success("Repository cloned", category=LogCategory.STATUS)
        except Exception as e:
            raise Exception(f"Git clone failed: {str(e)}")

        phase_tracker.mark(PHASE_CREDS_MATERIALISE, "Writing per-task clouds.yaml")
        clouds_config = PerTaskCloudsConfig(openstack_envelope, work_dir=repo_path)
        openstack_env = clouds_config.__enter__()
        task_logger.success("Per-task clouds.yaml written", category=LogCategory.STATUS)

        tofu_dir = _resolve_tofu_dir(repo_path)

        tofu = TofuExecutor(
            tofu_dir,
            env_vars=openstack_env,
            backend_conn_str=tfstate_conn_str,
            backend_schema_name=tfstate_schema,
            output_callback=_stream_line,
        )

        phase_tracker.mark(PHASE_TOFU_INIT, "Initializing OpenTofu")
        success, stdout, stderr = tofu.init()
        if not success:
            if stdout:
                task_logger.command_output("tofu_init_stdout", stdout, returncode=1)
            if stderr:
                task_logger.command_output("tofu_init_stderr", stderr, returncode=1)
            raise Exception("OpenTofu init failed")
        task_logger.success("OpenTofu initialization completed", category=LogCategory.STATUS)

        # Pull the canonical state from the pg backend, then walk it
        # to find every compute instance attached to this deployment.
        # An empty list usually means the deployment never reached a
        # successful apply — surface that as a hard error rather than
        # a silent no-op so the user doesn't think pause "worked" on
        # an empty deployment.
        state_dump = tofu.state_pull()
        server_ids = _extract_compute_instance_ids(state_dump)
        if not server_ids:
            raise Exception(
                "No compute instances found in tofu state — "
                f"nothing to {action}. The deployment may have been "
                "torn down already or never reached a successful apply."
            )
        task_logger.info(
            f"{len(server_ids)} compute instance(s) found",
            category=LogCategory.OPERATION,
            server_ids=server_ids,
        )

        phase_tracker.mark(
            server_phase,
            f"{'Stopping' if server_op == 'stop' else 'Starting'} {len(server_ids)} server(s)",
        )

        openstack_service = OpenStackService(env_vars=openstack_env)
        op_method = openstack_service.server_stop if server_op == "stop" else openstack_service.server_start

        failures: list[tuple[str, str]] = []
        for sid in server_ids:
            # Optional pre-flight: log the human name + power state so
            # the per-deployment log is readable. We never fail on
            # show() — it's purely cosmetic.
            info = openstack_service.server_show(sid)
            label_str = f"{info.get('name', sid)}" if info else sid
            current = info.get("status") if info else None
            task_logger.info(
                f"{server_op} {label_str} (status: {current or 'unknown'})",
                category=LogCategory.OPERATION,
                server_id=sid,
            )

            ok, err = op_method(sid)
            if ok:
                task_logger.success(
                    f"{label_str}: {server_op} OK",
                    category=LogCategory.STATUS,
                )
            else:
                task_logger.error(
                    f"{label_str}: {server_op} failed: {err}",
                    category=LogCategory.ERROR,
                    server_id=sid,
                )
                failures.append((sid, err or "unknown error"))

        if failures:
            joined = "; ".join(f"{sid}: {err}" for sid, err in failures)
            raise Exception(f"{action} failed for {len(failures)}/{len(server_ids)} server(s): {joined}")

        phase_tracker.mark(PHASE_CLEANUP, "Pulling final state snapshot")
        # State doesn't change for pause/resume (the resources still
        # exist, just in a different power state), but we pull it
        # again so the task row gets a fresh snapshot for debugging.
        try:
            tf_state_post = tofu.state_pull()
        except Exception as e:
            task_logger.warning(
                f"Could not pull tofu state post-{action}: {e}",
                category=LogCategory.WARNING,
            )
            tf_state_post = state_dump

        task_logger.success(
            f"Deployment {deployment_id} {action}d successfully",
            category=LogCategory.STATUS,
        )

        return {
            "status": "success",
            "deployment_id": deployment_id,
            "logs": task_logger.get_logs_dict(),
            "tf_state": tf_state_post,
            "commit_info": None,
            # Pause/resume don't generate or change tofu outputs —
            # field is kept for event-listener parity with the deploy
            # / destroy payload shape.
            "tofu_outputs": {},
        }

    except Exception as e:
        task_logger.exception(f"{action} failed: {str(e)}", exception=e, deployment_id=deployment_id)
        raise Failure(
            message=str(e),
            deployment_id=deployment_id,
            logs_dict=task_logger.get_logs_dict(),
            tf_state=None,
            commit_info=None,
            tofu_outputs={},
        )

    finally:
        _cleanup_task_resources(clouds_config, repo_path, task_logger)


@celery_app.task(bind=True, name="tasks.pause_deployment")
def pause_deployment(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] = None,
    openstack_envelope: dict[str, Any] | None = None,
):
    """Halt a deployment by stopping all of its compute instances.

    Volumes and networks are untouched, so resume restores the same
    instances byte-for-byte. The tofu state is also untouched,
    so a subsequent destroy proceeds normally (tofu destroy is
    happy to tear down SHUTOFF instances).
    """
    return _run_compute_lifecycle(
        self,
        deployment_id,
        app_id,
        app_git_link,
        release,
        user_vars,
        teams,
        openstack_envelope,
        action="pause",
        phases=_PHASES_PAUSE,
        server_phase=PHASE_SERVER_STOP,
        server_op="stop",
    )


@celery_app.task(bind=True, name="tasks.resume_deployment")
def resume_deployment(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] = None,
    openstack_envelope: dict[str, Any] | None = None,
):
    """Resume a paused deployment by starting all of its compute instances.

    Mirrors :func:`pause_deployment`'s preamble exactly so the two
    code paths stay symmetric and easy to compare side-by-side.
    """
    return _run_compute_lifecycle(
        self,
        deployment_id,
        app_id,
        app_git_link,
        release,
        user_vars,
        teams,
        openstack_envelope,
        action="resume",
        phases=_PHASES_RESUME,
        server_phase=PHASE_SERVER_START,
        server_op="start",
    )


# ----------------------------------------------------------------
# REDEPLOY ONE RESOURCE
# ----------------------------------------------------------------
#
# Replace exactly one compute instance via
# ``tofu apply -replace=<addr> -target=<addr>``. Everything else in
# the deployment stays untouched. The backend whitelists the address
# against the cached TF state before dispatch; we re-check the address
# shape here as defense in depth. Same per-task clouds.yaml + pg backend
# schema as deploy/destroy, so the apply sees the same state file.

_REDEPLOY_ADDRESS_RE = re.compile(
    r"""^
    [A-Za-z_][A-Za-z0-9_]*
    \.[A-Za-z_][A-Za-z0-9_-]*
    (?:\[(?:\d+|"[^"\\]+")\])?
    $""",
    re.VERBOSE,
)


def _build_current_roster(teams: dict[str, list]) -> tuple[set[str], set[str]]:
    """Compute the legal slot-key sets for the current roster.

    Returns a ``(team_keys, user_keys)`` tuple:

    * ``team_keys`` — every team name currently present. These are the
      valid slot keys for ``var_scope=team``.
    * ``user_keys`` — composite ``"<team>-<email>"`` keys for every
      user currently rostered to a team. These are the valid slot keys
      for ``var_scope=user``.

    Roster entries can either be plain strings (email addresses) or
    dicts with an ``email`` key — matches the shape the backend ships
    in ``teams`` (see ``_attach_files_to_user_input``). Anything else is
    skipped defensively.
    """
    team_keys: set[str] = set()
    user_keys: set[str] = set()
    for team_name, members in (teams or {}).items():
        if not team_name:
            continue
        team_keys.add(team_name)
        if not isinstance(members, list):
            continue
        for member in members:
            email = member if isinstance(member, str) else (member.get("email") if isinstance(member, dict) else None)
            if not email:
                continue
            user_keys.add(f"{team_name}-{email}")
    return team_keys, user_keys


def _reconcile_scoped_vars_to_roster(
    tofu_vars: dict[str, Any],
    teams: dict[str, list],
    task_logger: Any,
) -> dict[str, Any]:
    """Drop scoped-map entries whose slot keys no longer match the roster.

    Redeploy replays the originally-persisted ``user_vars["tofu"]``
    blob, but the team/user roster may have shifted since the initial
    deploy (members added or removed, teams renamed). A scoped variable
    keyed on the old roster would then ship OpenTofu a map containing
    orphan keys — at best a noisy diff, at worst a type/required
    failure that blocks the replace.

    Heuristic: a value is considered scoped when it's a non-empty
    ``dict`` whose keys form a subset of either the team-name roster
    (``var_scope=team``) or the ``<team>-<user>`` composite roster
    (``var_scope=user``). On match we intersect the value's keys with
    the current roster and drop the orphans. Maps that don't match the
    heuristic — e.g. file-shape vars or the ``users`` injection — are
    left untouched. Every drop is announced in the task log so the
    operator sees which slots were retired.

    A value that becomes empty after intersection is dropped from the
    var-set entirely; OpenTofu validation handles the
    missing-required case from there (it can pick up a declared
    default or surface the required-but-missing error properly).
    """
    if not tofu_vars:
        return tofu_vars

    team_keys, user_keys = _build_current_roster(teams)
    if not team_keys and not user_keys:
        # Nothing rostered — can't reconcile, leave the var-set alone.
        return tofu_vars

    reconciled: dict[str, Any] = {}
    for name, value in tofu_vars.items():
        # Only dict-shaped, non-empty values can be scoped maps. Skip
        # the ``users`` injection — we set that ourselves from ``teams``
        # right after this and it's not an app-defined scoped var.
        if name == "users" or not isinstance(value, dict) or not value:
            reconciled[name] = value
            continue
        # File-shape values (see ``_looks_like_file_var_value``) are
        # already keyed by slot but use a different content contract;
        # let the regular file-strip handle them.
        if _looks_like_file_var_value(value):
            reconciled[name] = value
            continue

        slot_keys = set(value.keys())
        # Pick the roster axis whose universe best matches the slot
        # keys. Subset wins outright; otherwise pick the axis with the
        # larger overlap so a partially-stale map still gets cleaned.
        team_overlap = slot_keys & team_keys
        user_overlap = slot_keys & user_keys
        if slot_keys <= team_keys and team_keys:
            allowed = team_keys
        elif slot_keys <= user_keys and user_keys or len(user_overlap) >= len(team_overlap) and user_overlap:
            allowed = user_keys
        elif team_overlap:
            allowed = team_keys
        else:
            # No overlap with either roster axis — leave the value
            # alone. Probably a non-scoped map(string,...) variable
            # the user explicitly populated.
            reconciled[name] = value
            continue

        kept = {k: v for k, v in value.items() if k in allowed}
        dropped = sorted(slot_keys - allowed)
        if dropped:
            task_logger.warning(
                f"Redeploy roster reconciliation: dropped {len(dropped)} "
                f"orphan slot(s) from variable '{name}': {dropped}",
                category=LogCategory.WARNING,
                variable=name,
                dropped_slots=dropped,
            )
        if kept:
            reconciled[name] = kept
        else:
            # All slots orphaned — drop the var entirely so tofu
            # validation can fall back to the declared default (if any)
            # or surface a proper required-but-missing error.
            task_logger.warning(
                f"Redeploy roster reconciliation: variable '{name}' has "
                "no surviving slots after roster intersection — falling "
                "back to its declared default (or required-but-missing).",
                category=LogCategory.WARNING,
                variable=name,
            )
    return reconciled


@celery_app.task(bind=True, name="tasks.redeploy_resource")
def redeploy_resource(
    self,
    deployment_id: str,
    app_id: str,
    app_git_link: str,
    release: str,
    user_vars: dict[str, Any],
    teams: dict[str, list] = None,
    openstack_envelope: dict[str, Any] | None = None,
    resource_address: str | None = None,
):
    """Replace ONE compute instance via ``-target`` + ``-replace``.

    Args mirror ``deploy_application`` so the backend's
    ``_dispatch_lifecycle_task`` can ship the same persisted state.
    The extra ``resource_address`` carries the OpenTofu state address
    (e.g. ``openstack_compute_instance_v2.team_ide["Team-A"]``) the
    user clicked.

    Returns the same payload shape as deploy/destroy so the celery
    event listener stays generic.
    """
    task_logger = get_logger(f"redeploy:{deployment_id}", correlation_id=deployment_id)

    bound_task = self

    def _emit(event_name: str, payload: dict[str, Any]) -> None:
        bound_task.send_event(event_name, deployment_id=deployment_id, **payload)

    task_logger.set_event_emitter(_emit)
    phase_tracker = _PhaseTracker(task_logger, _PHASES_REDEPLOY)

    repo_path: str | None = None
    tf_state: str | None = None
    outputs: dict[str, Any] | None = None
    commit_info: dict[str, Any] | None = None
    tofu_dir: str | None = None
    openstack_env: dict[str, str] = {}
    clouds_config: PerTaskCloudsConfig | None = None

    tfstate_conn_str = settings.TFSTATE_DATABASE_URL or None
    tfstate_schema = _tfstate_schema_name(deployment_id)

    if teams is None:
        teams = {}

    def _stream_line(tool: str, line: str) -> None:
        task_logger.tool_output_line(tool, line)

    def collect_tofu_state():
        return collect_tofu_state_helper(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger)

    def collect_tofu_outputs():
        return collect_tofu_outputs_helper(tofu_dir, openstack_env, tfstate_conn_str, tfstate_schema, task_logger)

    try:
        # Validate the address shape before we do any work. Backend
        # already whitelisted the address against the cached state, but
        # we double-check the shape so an empty / malformed string from
        # a misconfigured caller doesn't reach the CLI.
        if not resource_address or not _REDEPLOY_ADDRESS_RE.match(resource_address):
            raise Exception(f"redeploy_resource called with invalid resource_address: " f"{resource_address!r}")

        phase_tracker.mark(PHASE_STARTING, f"Starting redeploy of {resource_address}")
        task_logger.resource_info(
            "deployment",
            deployment_id,
            app_id=app_id,
            git_url=app_git_link,
            release=release,
            user_vars_keys=list(user_vars.keys()),
            teams_keys=list(teams.keys()),
            action="redeploy",
            resource_address=resource_address,
        )

        phase_tracker.mark(PHASE_OPENSTACK_SETUP, "Validating OpenStack credentials")
        if not openstack_envelope:
            raise Exception("OpenStack credential envelope missing — cannot redeploy without credentials")
        task_logger.success("OpenStack credential envelope received", category=LogCategory.STATUS)

        phase_tracker.mark(PHASE_GIT_CLONE, "Cloning repository at original release tag")
        task_logger.info(
            f"Cloning {app_git_link} at {release} (same ref as the original deploy "
            "so tofu code matches the pg-backend state)",
            category=LogCategory.OPERATION,
        )
        try:
            repo_path = git_service.clone_release(git_url=app_git_link, deployment_id=deployment_id, tag=release)
            try:
                commit_info = _extract_commit_info(repo_path)
                task_logger.success(
                    f"Repository cloned at commit {commit_info['hash'][:8]}",
                    category=LogCategory.STATUS,
                )
            except Exception as e:
                task_logger.warning(f"Could not extract commit info: {e}", category=LogCategory.WARNING)
        except Exception as e:
            raise Exception(f"Git clone failed: {str(e)}")

        phase_tracker.mark(PHASE_CREDS_MATERIALISE, "Writing per-task clouds.yaml")
        clouds_config = PerTaskCloudsConfig(openstack_envelope, work_dir=repo_path)
        openstack_env = clouds_config.__enter__()
        task_logger.success("Per-task clouds.yaml written", category=LogCategory.STATUS)

        tofu_dir = _resolve_tofu_dir(repo_path)

        # Build the tofu var-set like the original deploy. We KEEP
        # file variables here: ``tofu apply -replace`` recreates the
        # targeted VM, so cloud-init runs fresh and needs the original
        # ``write_files`` payload. The backend pre-filters these vars for
        # non-recreating lifecycles (destroy/pause/resume) — see
        # ``_dispatch_lifecycle_task`` in backend/app/routers/deployments.py.
        tofu_vars = {**user_vars["tofu"]} if "tofu" in user_vars else {}
        # The persisted ``user_vars`` were keyed on the roster at deploy
        # time. Membership may have shifted since (team renames, members
        # added/removed); ship the apply only the slots that still match
        # the current roster so tofu doesn't choke on orphan keys.
        tofu_vars = _reconcile_scoped_vars_to_roster(tofu_vars, teams, task_logger)
        if teams:
            tofu_vars["users"] = teams
        tofu_vars = encode_tofu_vars(tofu_vars)

        tofu = TofuExecutor(
            tofu_dir,
            env_vars=openstack_env,
            backend_conn_str=tfstate_conn_str,
            backend_schema_name=tfstate_schema,
            output_callback=_stream_line,
        )

        phase_tracker.mark(PHASE_TOFU_INIT, "Initializing OpenTofu")
        success, stdout, stderr = tofu.init()
        if not success:
            if stdout:
                task_logger.command_output("tofu_init_stdout", stdout, returncode=1)
            if stderr:
                task_logger.command_output("tofu_init_stderr", stderr, returncode=1)
            raise Exception("OpenTofu init failed")
        task_logger.success("OpenTofu initialization completed", category=LogCategory.STATUS)

        phase_tracker.mark(
            PHASE_TOFU_APPLY,
            f"Applying replace for {resource_address}",
        )
        # ``-replace`` taints the single resource so tofu plans a
        # destroy+create on it; ``-target`` scopes the apply to that
        # resource (and its dependencies) so the rest of the graph is
        # untouched.
        success, stdout, stderr = tofu.apply(
            variables=tofu_vars,
            targets=[resource_address],
            replace=[resource_address],
        )
        if not success:
            if stdout:
                task_logger.command_output("tofu_apply_stdout", stdout, returncode=1)
            if stderr:
                task_logger.command_output("tofu_apply_stderr", stderr, returncode=1)
            raise Exception("OpenTofu apply (replace) failed")
        task_logger.success(
            f"Resource {resource_address} replaced",
            category=LogCategory.STATUS,
        )

        phase_tracker.mark(PHASE_CLEANUP, "Pulling final state")
        tf_state = collect_tofu_state()
        outputs = collect_tofu_outputs()

        task_logger.success(
            f"Deployment {deployment_id} redeploy of {resource_address} completed",
            category=LogCategory.STATUS,
        )

        return {
            "status": "success",
            "deployment_id": deployment_id,
            "logs": task_logger.get_logs_dict(),
            "tf_state": tf_state,
            "commit_info": commit_info,
            "tofu_outputs": outputs or {},
        }

    except Exception as e:
        task_logger.exception(f"Redeploy failed: {str(e)}", exception=e, deployment_id=deployment_id)
        if not tf_state:
            tf_state = collect_tofu_state()
        raise Failure(
            message=str(e),
            deployment_id=deployment_id,
            logs_dict=task_logger.get_logs_dict(),
            tf_state=tf_state,
            commit_info=commit_info,
            tofu_outputs=outputs or {},
        )

    finally:
        _cleanup_task_resources(clouds_config, repo_path, task_logger)
