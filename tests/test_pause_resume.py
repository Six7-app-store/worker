"""Tests for the pause/resume helpers in worker/app/tasks.py."""

import json

import pytest


@pytest.mark.unit
class TestComputeInstanceExtractor:
    """Walk realistic tofu state shapes and surface the server IDs."""

    def test_extracts_compute_instance_ids(self):
        from app.tasks import _extract_compute_instance_ids

        state = {
            "resources": [
                {
                    "type": "openstack_compute_instance_v2",
                    "instances": [
                        {"attributes": {"id": "srv-1", "name": "web"}},
                        {"attributes": {"id": "srv-2", "name": "db"}},
                    ],
                },
                # A volume should NOT be returned — pause/resume only
                # touches Nova instances.
                {
                    "type": "openstack_blockstorage_volume_v3",
                    "instances": [{"attributes": {"id": "vol-9"}}],
                },
                {
                    "type": "openstack_compute_instance_v2",
                    "instances": [{"attributes": {"id": "srv-3"}}],
                },
            ]
        }
        ids = _extract_compute_instance_ids(json.dumps(state))
        assert ids == ["srv-1", "srv-2", "srv-3"]

    def test_returns_empty_list_for_empty_state(self):
        from app.tasks import _extract_compute_instance_ids

        assert _extract_compute_instance_ids(None) == []
        assert _extract_compute_instance_ids("") == []
        assert _extract_compute_instance_ids("{}") == []
        assert _extract_compute_instance_ids('{"resources": []}') == []

    def test_returns_empty_list_for_invalid_json(self):
        """A best-effort parse — never raises, just yields no IDs."""
        from app.tasks import _extract_compute_instance_ids

        assert _extract_compute_instance_ids("not json") == []
        assert _extract_compute_instance_ids("{not even close") == []

    def test_skips_instances_without_id(self):
        from app.tasks import _extract_compute_instance_ids

        state = {
            "resources": [
                {
                    "type": "openstack_compute_instance_v2",
                    "instances": [
                        {"attributes": {"id": "srv-1"}},
                        {"attributes": {}},  # malformed — skip
                        {},  # missing attributes — skip
                    ],
                }
            ]
        }
        ids = _extract_compute_instance_ids(json.dumps(state))
        assert ids == ["srv-1"]

    def test_accepts_dict_input(self):
        """The helper accepts both serialised JSON and pre-parsed dicts.

        ``tofu.state_pull()`` returns a string, but tests find it
        easier to pass a dict directly. Supporting both keeps the
        helper friendly without making callers serialise first.
        """
        from app.tasks import _extract_compute_instance_ids

        state = {
            "resources": [
                {
                    "type": "openstack_compute_instance_v2",
                    "instances": [{"attributes": {"id": "srv-1"}}],
                }
            ]
        }
        assert _extract_compute_instance_ids(state) == ["srv-1"]
