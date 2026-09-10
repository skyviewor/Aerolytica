"""Focused tests for the versioned cloud-sync cursor path."""

import pytest

from aero.core.cloud_sync import CloudSyncEngine


@pytest.mark.asyncio
async def test_remote_changes_updates_persisted_cursor_and_merges_entities():
    engine = object.__new__(CloudSyncEngine)
    engine._state = {
        "binding": {
            "project_id": "project_1",
            "root_directory_id": None,
            "includes": ["papers/**"],
        },
        "remote_cursor": "cursor-1",
        "remote_entities": {
            "directory:directory_1": {
                "directory_id": "directory_1",
                "project_id": "project_1",
                "parent_directory_id": None,
                "name": "papers",
                "status": "active",
                "deleted_at": None,
            },
            "file:file_1": {
                "file_id": "file_1",
                "project_id": "project_1",
                "directory_id": "directory_1",
                "filename": "old.md",
                "status": "active",
                "deleted_at": None,
            },
        },
    }

    async def api(method, path, **kwargs):
        assert method == "GET"
        assert path == "/changes"
        assert kwargs["params"] == {
            "project_id": "project_1",
            "limit": 200,
            "cursor": "cursor-1",
        }
        return {
            "items": [{
                "entity_type": "file",
                "entity_id": "file_2",
                "operation": "upsert",
                "data": {
                    "file_id": "file_2",
                    "project_id": "project_1",
                    "directory_id": "directory_1",
                    "filename": "new.md",
                    "status": "active",
                    "deleted_at": None,
                },
            }],
            "has_more": False,
            "cursor": "cursor-2",
            "next_cursor": None,
        }

    engine._api = api
    result = await engine._remote()

    assert engine._state["remote_cursor"] == "cursor-2"
    assert set(result) == {"papers", "papers/old.md", "papers/new.md"}
    assert engine._state["remote_entities"]["file:file_2"]["filename"] == "new.md"
