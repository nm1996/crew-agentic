"""Locate, load, validate and write ``.crew/config.yaml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from crew.config.schema import CrewConfig
from crew.domain.errors import ConfigError, NotInitializedError

CREW_DIR = ".crew"
CONFIG_FILE = "config.yaml"
DB_FILE = "crew.db"


@dataclass(frozen=True)
class ProjectPaths:
    """Filesystem layout of a CREW-initialised repository."""

    root: Path

    @property
    def crew_dir(self) -> Path:
        return self.root / CREW_DIR

    @property
    def config_file(self) -> Path:
        return self.crew_dir / CONFIG_FILE

    @property
    def db_file(self) -> Path:
        return self.crew_dir / DB_FILE

    @property
    def runs_dir(self) -> Path:
        return self.crew_dir / "runs"

    @property
    def context_dir(self) -> Path:
        return self.crew_dir / "context"

    @property
    def logs_dir(self) -> Path:
        return self.crew_dir / "logs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` until a directory containing ``.crew/config.yaml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CREW_DIR / CONFIG_FILE).is_file():
            return candidate
    raise NotInitializedError(
        "No CREW project found in this directory or its parents.",
        hint="Run `crew init` in the root of your Git repository.",
    )


def load_config(paths: ProjectPaths) -> CrewConfig:
    try:
        raw = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotInitializedError(f"Missing {paths.config_file}", hint="Run `crew init`.") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{paths.config_file} is not valid YAML: {exc}") from exc
    return parse_config(raw, source=str(paths.config_file))


def parse_config(raw: Any, *, source: str = "config") -> CrewConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source} must contain a YAML mapping")
    try:
        return CrewConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration in {source}: {problems}") from exc


def dump_config(config: CrewConfig) -> str:
    data = config.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_config(paths: ProjectPaths, config: CrewConfig) -> None:
    paths.config_file.write_text(dump_config(config), encoding="utf-8")
