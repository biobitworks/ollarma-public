"""
Tests for harness/registry.py

TDD RED phase — these tests must FAIL before registry.py is implemented.
"""
import pathlib
import tempfile
import textwrap
import pytest

from ollarma.registry import load_models, load_tasks, ModelConfig, TaskConfig


# ---------------------------------------------------------------------------
# ModelConfig tests
# ---------------------------------------------------------------------------

def test_model_config_required_fields():
    """ModelConfig must accept name only."""
    m = ModelConfig(name="qwen3:8b")
    assert m.name == "qwen3:8b"
    assert m.description is None


def test_model_config_with_description():
    """ModelConfig accepts optional description."""
    m = ModelConfig(name="phi4-mini", description="Phi4 Mini 3.8B")
    assert m.name == "phi4-mini"
    assert m.description == "Phi4 Mini 3.8B"


# ---------------------------------------------------------------------------
# TaskConfig tests
# ---------------------------------------------------------------------------

def test_task_config_required_fields():
    """TaskConfig must accept all four required fields."""
    t = TaskConfig(id="t01", suite="science", prompt="What?", num_ctx=4096)
    assert t.id == "t01"
    assert t.suite == "science"
    assert t.prompt == "What?"
    assert t.num_ctx == 4096
    assert t.expected is None


def test_task_config_with_expected():
    """TaskConfig accepts optional expected field."""
    t = TaskConfig(id="t02", suite="code", prompt="Write fn", num_ctx=4096, expected="def fn(): pass")
    assert t.expected == "def fn(): pass"


# ---------------------------------------------------------------------------
# load_models() tests
# ---------------------------------------------------------------------------

def test_load_models_returns_list_of_model_config(tmp_path):
    """load_models() returns a list of ModelConfig objects."""
    yml = tmp_path / "models.yml"
    yml.write_text(textwrap.dedent("""\
        models:
          - name: qwen3:8b
            description: "Primary tier"
          - name: phi4-mini
    """))
    models = load_models(str(yml))
    assert isinstance(models, list)
    assert len(models) == 2
    assert all(isinstance(m, ModelConfig) for m in models)


def test_load_models_name_field(tmp_path):
    """load_models() correctly parses name fields."""
    yml = tmp_path / "models.yml"
    yml.write_text("models:\n  - name: qwen3:8b\n  - name: phi4-mini\n")
    models = load_models(str(yml))
    names = [m.name for m in models]
    assert "qwen3:8b" in names
    assert "phi4-mini" in names


def test_load_models_file_not_found():
    """load_models() raises FileNotFoundError with clear message when file missing."""
    with pytest.raises(FileNotFoundError, match="models.yml"):
        load_models("/nonexistent/path/models.yml")


def test_load_models_missing_models_key(tmp_path):
    """load_models() raises ValueError when 'models' key is absent."""
    yml = tmp_path / "models.yml"
    yml.write_text("not_models:\n  - name: qwen3:8b\n")
    with pytest.raises(ValueError, match="models"):
        load_models(str(yml))


def test_load_models_project_models_yml():
    """load_models() reads the actual project models.yml successfully."""
    models = load_models("models.yml")
    assert len(models) >= 2
    names = [m.name for m in models]
    assert "qwen3.5:9b" in names
    assert "phi4-mini" in names


# ---------------------------------------------------------------------------
# load_tasks() tests
# ---------------------------------------------------------------------------

def _write_task(directory: pathlib.Path, suite: str, task_id: str, num_ctx: int = 4096) -> pathlib.Path:
    """Helper: write a minimal valid task YAML to a suite subdirectory."""
    suite_dir = directory / suite
    suite_dir.mkdir(parents=True, exist_ok=True)
    task_file = suite_dir / f"{task_id}.yml"
    task_file.write_text(textwrap.dedent(f"""\
        id: {task_id}
        suite: {suite}
        num_ctx: {num_ctx}
        expected: null
        prompt: |
          Test prompt for {task_id}
    """))
    return task_file


def test_load_tasks_returns_list_of_task_config(tmp_path):
    """load_tasks() returns a list of TaskConfig objects."""
    _write_task(tmp_path, "science", "t01")
    tasks = load_tasks(str(tmp_path))
    assert isinstance(tasks, list)
    assert len(tasks) == 1
    assert isinstance(tasks[0], TaskConfig)


def test_load_tasks_discovers_multiple_suites(tmp_path):
    """load_tasks() globs recursively across all suite subdirectories."""
    _write_task(tmp_path, "science", "sci_01")
    _write_task(tmp_path, "code", "code_01")
    _write_task(tmp_path, "swarm", "swarm_01", num_ctx=8192)
    tasks = load_tasks(str(tmp_path))
    assert len(tasks) == 3
    suites = {t.suite for t in tasks}
    assert suites == {"science", "code", "swarm"}


def test_load_tasks_sorted_by_suite_then_id(tmp_path):
    """load_tasks() returns tasks sorted by (suite, id) for deterministic ordering."""
    _write_task(tmp_path, "science", "sci_02")
    _write_task(tmp_path, "science", "sci_01")
    _write_task(tmp_path, "code", "code_01")
    tasks = load_tasks(str(tmp_path))
    keys = [(t.suite, t.id) for t in tasks]
    assert keys == sorted(keys), f"Tasks not sorted: {keys}"


def test_load_tasks_skips_invalid_yaml(tmp_path):
    """load_tasks() skips YAML files missing required fields (logs warning, no crash)."""
    # Valid task
    _write_task(tmp_path, "science", "valid_01")
    # Invalid task (missing 'suite' field)
    bad_dir = tmp_path / "code"
    bad_dir.mkdir()
    bad_file = bad_dir / "bad_task.yml"
    bad_file.write_text("id: bad\nnum_ctx: 4096\nprompt: |\n  Test\n")  # missing suite
    tasks = load_tasks(str(tmp_path))
    # Only the valid task should be returned
    assert len(tasks) == 1
    assert tasks[0].id == "valid_01"


def test_load_tasks_project_tasks_dir():
    """load_tasks() reads the actual project tasks/ directory successfully."""
    tasks = load_tasks("tasks")
    assert len(tasks) >= 3
    suites = {t.suite for t in tasks}
    assert suites == {"science", "code", "swarm"}


def test_load_tasks_project_num_ctx_values():
    """Science and code tasks use num_ctx=4096; swarm uses num_ctx=8192."""
    tasks = load_tasks("tasks")
    for t in tasks:
        if t.suite in ("science", "code"):
            assert t.num_ctx == 4096, f"{t.id} expected 4096, got {t.num_ctx}"
        elif t.suite == "swarm":
            assert t.num_ctx == 8192, f"{t.id} expected 8192, got {t.num_ctx}"


def test_load_tasks_uses_glob_not_hardcoded(tmp_path):
    """Verify that adding a new task YAML is auto-discovered without code changes."""
    _write_task(tmp_path, "science", "existing_01")
    tasks_before = load_tasks(str(tmp_path))
    assert len(tasks_before) == 1

    # Add a new task — no code change, just a file
    _write_task(tmp_path, "science", "new_task_99")
    tasks_after = load_tasks(str(tmp_path))
    assert len(tasks_after) == 2
    ids = [t.id for t in tasks_after]
    assert "new_task_99" in ids
