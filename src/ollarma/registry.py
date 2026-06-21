"""
harness/registry.py — YAML-driven model and task registry.

Extension contract:
  - New model: append a "- name: <ollama-tag>" entry to models.yml. Zero Python changes.
  - New task:  add a YAML file under tasks/<suite>/. Zero Python changes.

Security: yaml.safe_load() is used exclusively (T-02-01, T-02-02).
"""
import logging
import pathlib
from typing import Optional

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ModelConfig(BaseModel):
    """Pydantic model for a single model declaration in models.yml.

    Fields:
        name: Ollama tag used for API calls (required, D-08).
        description: Human-readable label (optional, D-10).
    """

    name: str
    description: Optional[str] = None


class TaskConfig(BaseModel):
    """Pydantic model for a single task YAML file.

    Fields (D-11):
        id: Unique task identifier.
        suite: Workload suite — "science" | "code" | "swarm".
        prompt: Prompt text sent to the model.
        num_ctx: Context window size (task-level, D-13).
        expected: Optional ground-truth for scoring (optional in Phase 1, D-12).
        num_predict: Optional task-level decode-token cap. None falls back to
            executor.DEFAULT_NUM_PREDICT so verbose/thinking models stay bounded.
    """

    id: str
    suite: str
    prompt: str
    num_ctx: int
    expected: Optional[str] = None
    num_predict: Optional[int] = None


def load_models(path: str = "models.yml") -> list[ModelConfig]:
    """Load model declarations from a YAML file.

    Args:
        path: Path to the models YAML file. Defaults to "models.yml".

    Returns:
        List of ModelConfig objects, one per model entry.

    Raises:
        FileNotFoundError: If the file at *path* does not exist.
        ValueError: If the file does not contain a top-level "models" key.
    """
    models_path = pathlib.Path(path)
    if not models_path.exists():
        raise FileNotFoundError(f"models.yml not found at {path}")

    # yaml.safe_load() prevents arbitrary Python object instantiation (T-02-01, T-02-02)
    data = yaml.safe_load(models_path.read_text())

    if "models" not in data:
        raise ValueError(f"models.yml missing 'models' key (got keys: {list(data.keys())})")

    return [ModelConfig(**m) for m in data["models"]]


_REQUIRED_TASK_FIELDS = {"id", "suite", "prompt", "num_ctx"}


def load_tasks(tasks_dir: str = "tasks") -> list[TaskConfig]:
    """Discover and load all task YAML files under *tasks_dir* recursively.

    Uses pathlib glob — no hardcoded task paths. Adding a new task YAML file
    under tasks/<suite>/ is automatically picked up with zero code changes.

    Args:
        tasks_dir: Root directory to search for task YAML files. Defaults to "tasks".

    Returns:
        List of TaskConfig objects sorted by (suite, id) for deterministic ordering.
        Files with missing required fields are skipped (logged as warnings).
    """
    root = pathlib.Path(tasks_dir)
    tasks: list[TaskConfig] = []

    for yml_path in root.glob("**/*.yml"):
        try:
            data = yaml.safe_load(yml_path.read_text())
        except yaml.YAMLError as exc:
            logger.warning("Skipping %s: YAML parse error — %s", yml_path, exc)
            continue

        if not isinstance(data, dict):
            logger.warning("Skipping %s: YAML root is not a mapping", yml_path)
            continue

        missing = _REQUIRED_TASK_FIELDS - data.keys()
        if missing:
            logger.warning("Skipping %s: missing fields %s", yml_path, sorted(missing))
            continue

        try:
            tasks.append(TaskConfig(**data))
        except Exception as exc:  # noqa: BLE001 — catch pydantic validation errors
            logger.warning("Skipping %s: validation error — %s", yml_path, exc)
            continue

    # Sort for deterministic ordering (suite, then id) regardless of filesystem order
    tasks.sort(key=lambda t: (t.suite, t.id))
    return tasks
