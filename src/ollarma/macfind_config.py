"""macfind_config.py — find_on_mac config loader."""
from __future__ import annotations
import pathlib
from pydantic import BaseModel
import orjson

MACFIND_CONFIG_PATH = pathlib.Path.home() / ".config" / "ollarma" / "macfind.json"
MACFIND_NOT_CONFIGURED = "MACFIND_NOT_CONFIGURED"


class MacFindConfig(BaseModel):
    model_config = {"frozen": True}
    approved_dirs: tuple[pathlib.Path, ...]
    confidence_threshold: float = 0.25
    max_results: int = 10


def load_macfind_config(config_path: pathlib.Path | None = None) -> MacFindConfig:
    """Load macfind config. Raises ValueError(MACFIND_NOT_CONFIGURED) if missing."""
    path = config_path or MACFIND_CONFIG_PATH
    if not path.exists():
        raise ValueError(MACFIND_NOT_CONFIGURED)
    raw = orjson.loads(path.read_bytes())
    dirs = [pathlib.Path(d).expanduser().resolve() for d in raw.get("approved_dirs", [])]
    if not dirs:
        raise ValueError(MACFIND_NOT_CONFIGURED)
    return MacFindConfig(
        approved_dirs=tuple(dirs),
        confidence_threshold=float(raw.get("confidence_threshold", 0.25)),
        max_results=int(raw.get("max_results", 10)),
    )
