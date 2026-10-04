import subprocess

import pytest

from aero.checkpoints import CheckpointError, CheckpointManager


def test_checkpoint_restores_controlled_files_without_touching_user_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    user_head = project / ".git" / "HEAD"
    user_head.write_text("ref: refs/heads/main\n")
    script = project / "scripts" / "plot.py"
    script.parent.mkdir()
    script.write_text("print('first')\n")
    data = project / "data" / "sample.nc"
    data.parent.mkdir()
    data.write_bytes(b"CDF-data-v1")

    manager = CheckpointManager(project)
    checkpoint = manager.create("baseline", session_snapshot=b"encrypted")
    script.write_text("print('second')\n")
    data.write_bytes(b"CDF-data-v2")
    added = project / "notes.md"
    added.write_text("later\n")

    diff = manager.diff(checkpoint["id"])
    assert diff.modified == ("scripts/plot.py",)
    assert diff.added == ("notes.md",)
    assert diff.references_changed == ("data/sample.nc",)

    manager.restore(checkpoint["id"])

    assert script.read_text() == "print('first')\n"
    assert data.read_bytes() == b"CDF-data-v2"
    assert not added.exists()
    assert user_head.read_text() == "ref: refs/heads/main\n"
    assert manager.session_snapshot(checkpoint["id"]) == b"encrypted"
    assert manager._read_state()["experiment"] == "恢复自「baseline」"


def test_checkpoint_does_not_copy_referenced_data(tmp_path):
    project = tmp_path / "project"
    data = project / "data" / "large.grib2"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"GRIB" * 1024)

    manager = CheckpointManager(project)
    checkpoint = manager.create("data reference")

    entry = next(item for item in checkpoint["files"] if item["path"] == "data/large.grib2")
    assert entry["restore"] == "reference"
    assert not list((project / ".aero" / "checkpoints" / checkpoint["id"]).glob("*.grib2"))


def test_checkpoint_excludes_secret_files_from_manifest_and_history(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "secrets.yaml").write_text("api_key: do-not-store\n")
    (project / ".env").write_text("TOKEN=do-not-store\n")
    (project / "aero.yaml").write_text("language: zh\n")

    checkpoint = CheckpointManager(project).create("safe")

    paths = {item["path"] for item in checkpoint["files"]}
    assert "aero.yaml" in paths
    assert "secrets.yaml" not in paths
    assert ".env" not in paths


def test_checkpoint_degrades_when_git_is_unavailable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("hello\n")
    manager = CheckpointManager(project, git_binary="definitely-not-a-git-binary")

    checkpoint = manager.create("manifest only")

    assert checkpoint["exact_restore"] is False
    assert checkpoint["commit"] is None
    with pytest.raises(CheckpointError, match="没有可用的文件快照"):
        manager.restore(checkpoint["id"])


def test_start_experiment_tracks_current_checkpoint_as_base(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manager = CheckpointManager(project)
    checkpoint = manager.create("base")

    state = manager.start_experiment("ozone sensitivity")

    assert state["experiment"] == "ozone sensitivity"
    assert state["experiment_base"] == checkpoint["id"]


def test_delete_checkpoint_repairs_children_and_current_pointer(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    notes = project / "notes.md"
    manager = CheckpointManager(project)

    notes.write_text("one\n")
    first = manager.create("first")
    notes.write_text("two\n")
    middle = manager.create("middle")
    notes.write_text("three\n")
    last = manager.create("last")

    deleted_middle = manager.delete(middle["id"])
    reloaded_last = manager.load(last["id"])

    assert deleted_middle["id"] == middle["id"]
    assert manager.load(middle["id"]) is None
    assert reloaded_last["parent_id"] == first["id"]
    assert manager._read_state()["current_checkpoint"] == last["id"]

    manager.delete(last["id"])

    assert manager._read_state()["current_checkpoint"] == first["id"]
    notes.write_text("changed\n")
    manager.restore(first["id"])
    assert notes.read_text() == "one\n"


def test_delete_checkpoint_removes_session_snapshot_and_private_ref(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("content\n")
    manager = CheckpointManager(project)
    checkpoint = manager.create("with session", session_snapshot=b"encrypted")

    manager.delete(checkpoint["id"])

    assert not (manager.checkpoints_dir / checkpoint["id"]).exists()
    assert manager.session_snapshot(checkpoint["id"]) is None
    result = subprocess.run(
        [
            "git",
            f"--git-dir={manager.history_dir}",
            "show-ref",
            "--verify",
            f"refs/checkpoints/{checkpoint['id']}",
        ],
        capture_output=True,
    )
    assert result.returncode != 0


def test_rename_checkpoint_only_updates_its_name(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("content\n")
    manager = CheckpointManager(project)
    checkpoint = manager.create("old name", session_snapshot=b"encrypted")

    renamed = manager.rename(checkpoint["id"], "  new analysis name  ")

    assert renamed["name"] == "new analysis name"
    assert renamed["id"] == checkpoint["id"]
    assert renamed["commit"] == checkpoint["commit"]
    assert renamed["parent_id"] == checkpoint["parent_id"]
    assert renamed["created_at"] == checkpoint["created_at"]
    assert manager.session_snapshot(checkpoint["id"]) == b"encrypted"
    assert manager.load(checkpoint["id"])["name"] == "new analysis name"


def test_rename_checkpoint_rejects_empty_name(tmp_path):
    manager = CheckpointManager(tmp_path)
    checkpoint = manager.create("original")

    with pytest.raises(CheckpointError, match="名称不能为空"):
        manager.rename(checkpoint["id"], "   ")


def test_clear_checkpoints_removes_history_without_touching_project_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "analysis.py"
    script.write_text("print('analysis')\n")
    data = project / "data" / "input.nc"
    data.parent.mkdir()
    data.write_bytes(b"CDF-data")
    manager = CheckpointManager(project)
    first = manager.create("first", session_snapshot=b"encrypted")
    safety = manager.create("safety", kind="pre-restore")

    removed = manager.clear()

    assert {item["id"] for item in removed} == {first["id"], safety["id"]}
    assert manager.list() == []
    assert not manager.checkpoints_dir.exists()
    assert not manager.history_dir.exists()
    assert manager._read_state()["current_checkpoint"] is None
    assert manager._read_state()["experiment_base"] is None
    assert script.read_text() == "print('analysis')\n"
    assert data.read_bytes() == b"CDF-data"


def test_experiment_checkpoint_is_scoped_to_experiment_workspace(tmp_path):
    project = tmp_path / "project"
    workspace = project / "experiments" / "trial"
    script = workspace / "scripts" / "analyze.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('first')\n")
    (project / "main.py").write_text("print('main')\n")

    manager = CheckpointManager(workspace, experiment_id="exp-1")
    checkpoint = manager.create("step one")

    assert checkpoint["scope"] == "experiment"
    assert checkpoint["experiment_id"] == "exp-1"
    assert {item["path"] for item in checkpoint["files"]} == {"scripts/analyze.py"}
    assert manager.checkpoints_dir == workspace / ".aero" / "checkpoints"


def test_main_checkpoint_excludes_experiment_workspaces(tmp_path):
    project = tmp_path / "project"
    experiment_script = project / "experiments" / "trial" / "scripts" / "test.py"
    experiment_script.parent.mkdir(parents=True)
    experiment_script.write_text("print('experiment')\n")
    (project / "main.py").write_text("print('main')\n")

    checkpoint = CheckpointManager(project).create(
        "experiment finished",
        kind="experiment-finish",
        related_experiment={"id": "exp-1", "name": "trial", "report": "report.md"},
    )

    assert {item["path"] for item in checkpoint["files"]} == {"main.py"}
    assert checkpoint["scope"] == "main"
    assert checkpoint["related_experiment"]["id"] == "exp-1"


def test_legacy_main_checkpoint_ignores_experiment_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('main')\n")
    manager = CheckpointManager(project)
    checkpoint = manager.create("legacy")
    metadata_path = manager.checkpoints_dir / checkpoint["id"] / "metadata.json"
    metadata = manager.load(checkpoint["id"])
    metadata["files"].append(
        {
            "path": "experiments/old/scripts/run.py",
            "restore": "exact",
            "fingerprint": "old",
        }
    )
    manager._write_json(metadata_path, metadata)

    diff = manager.diff(checkpoint["id"])

    assert "experiments/old/scripts/run.py" not in diff.missing
