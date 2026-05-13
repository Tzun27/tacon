"""Multi-classroom config — ``~/.tacon/classes.toml`` index.

A single user often TAs across multiple classrooms (different terms,
different courses) and each classroom has its own DB. v0.1 + v0.2
assumed one DB at ``~/.tacon/tacon.db``; v0.2.1 adds a thin index file
so the user can switch between named classrooms with ``--classroom <id>``.

Backwards compatible: if ``classes.toml`` doesn't exist, every CLI call
still resolves to the legacy default DB. The opt-in moment is when the
user runs ``tacon classroom add <id> --db PATH`` for the first time.

File format:

    default = "cs101-spring"

    [classrooms.cs101-spring]
    db_path = "~/.tacon/cs101-spring.db"
    description = "CS101 Spring 2026"

    [classrooms.cs101-fall]
    db_path = "~/.tacon/cs101-fall.db"
    description = "CS101 Fall 2025"

Read via stdlib ``tomllib`` on Python 3.11+, ``tomli`` on 3.10. Write
via a small hand-rolled serializer (the file is tiny — flat-ish and
under a kilobyte even with a dozen classrooms — so a manual writer
keeps the dep count down and the output stable).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback path
    import tomli as tomllib


CLASSES_FILENAME = "classes.toml"


class ClassesConfigError(Exception):
    """Raised when the classes.toml file is present but malformed."""


@dataclass(frozen=True)
class Classroom:
    """One classroom entry from ``classes.toml``."""

    id: str
    db_path: str  # may contain ~ — call expanded_db_path() to resolve
    description: str = ""

    def expanded_db_path(self) -> Path:
        return Path(self.db_path).expanduser()


@dataclass(frozen=True)
class ClassesConfig:
    """Parsed ``classes.toml``: default classroom id + the keyed map."""

    default: str | None = None
    classrooms: dict[str, Classroom] = field(default_factory=dict)

    def get(self, classroom_id: str) -> Classroom | None:
        return self.classrooms.get(classroom_id)

    def get_default(self) -> Classroom | None:
        if not self.default:
            return None
        return self.classrooms.get(self.default)

    def list_ids(self) -> list[str]:
        return sorted(self.classrooms.keys())


def _classes_path(home_dir: Path | None = None) -> Path:
    """The on-disk location of ``classes.toml``.

    Honors the ``TACON_HOME`` env var the same way ``cli._default_db_path``
    does, so passing ``home_dir`` is reserved for tests.
    """
    if home_dir is not None:
        return home_dir / CLASSES_FILENAME
    import os

    base = Path(os.environ.get("TACON_HOME", Path.home() / ".tacon"))
    return base / CLASSES_FILENAME


def load_classes(home_dir: Path | None = None) -> ClassesConfig | None:
    """Read ``classes.toml`` if present; return None if not.

    The absent-file case is the v0.2 (single-DB) default: callers should
    fall back to ``~/.tacon/tacon.db``. A present-but-malformed file
    raises :class:`ClassesConfigError` so the user notices, rather than
    silently dropping to legacy behavior.
    """
    path = _classes_path(home_dir)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ClassesConfigError(f"{path}: invalid TOML — {e}") from e

    default = raw.get("default")
    if default is not None and not isinstance(default, str):
        raise ClassesConfigError(
            f"{path}: 'default' must be a string, got {type(default).__name__}"
        )

    classrooms_raw = raw.get("classrooms", {})
    if not isinstance(classrooms_raw, dict):
        raise ClassesConfigError(
            f"{path}: 'classrooms' must be a table, got {type(classrooms_raw).__name__}"
        )

    classrooms: dict[str, Classroom] = {}
    for cid, entry in classrooms_raw.items():
        if not isinstance(entry, dict):
            raise ClassesConfigError(
                f"{path}: classrooms.{cid} must be a table, got {type(entry).__name__}"
            )
        db_path = entry.get("db_path")
        if not isinstance(db_path, str) or not db_path:
            raise ClassesConfigError(
                f"{path}: classrooms.{cid}.db_path is required (non-empty string)"
            )
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ClassesConfigError(
                f"{path}: classrooms.{cid}.description must be a string"
            )
        classrooms[cid] = Classroom(id=cid, db_path=db_path, description=description)

    if default is not None and default not in classrooms:
        raise ClassesConfigError(
            f"{path}: default = {default!r} but no [classrooms.{default}] block is defined"
        )

    return ClassesConfig(default=default, classrooms=classrooms)


def _serialize(config: ClassesConfig) -> str:
    """Hand-rolled TOML writer. Output is stable so diffs are clean."""
    lines: list[str] = []
    if config.default is not None:
        lines.append(f'default = "{_escape(config.default)}"')
        lines.append("")
    for cid in sorted(config.classrooms.keys()):
        c = config.classrooms[cid]
        lines.append(f"[classrooms.{cid}]")
        lines.append(f'db_path = "{_escape(c.db_path)}"')
        if c.description:
            lines.append(f'description = "{_escape(c.description)}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _escape(value: str) -> str:
    """Escape a TOML basic-string value. Conservative — backslashes + quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _save(config: ClassesConfig, home_dir: Path | None = None) -> Path:
    """Write the config to disk. Returns the resolved path."""
    path = _classes_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize(config), encoding="utf-8")
    return path


def add_classroom(
    *,
    classroom_id: str,
    db_path: str,
    description: str = "",
    make_default: bool = False,
    home_dir: Path | None = None,
) -> ClassesConfig:
    """Add or replace a classroom entry. Optionally mark it the default.

    Creates ``classes.toml`` if it doesn't exist. Re-adding an existing
    id is allowed (overwrites the entry); use this path to update a
    classroom's ``db_path`` or ``description``.
    """
    config = load_classes(home_dir) or ClassesConfig()
    new_classrooms = dict(config.classrooms)
    new_classrooms[classroom_id] = Classroom(
        id=classroom_id, db_path=db_path, description=description
    )
    default = classroom_id if make_default or config.default is None else config.default
    new_config = ClassesConfig(default=default, classrooms=new_classrooms)
    _save(new_config, home_dir)
    return new_config


def set_default(classroom_id: str, home_dir: Path | None = None) -> ClassesConfig:
    """Change which classroom is the default. Raises if the id is unknown."""
    config = load_classes(home_dir)
    if config is None or classroom_id not in config.classrooms:
        raise ClassesConfigError(
            f"unknown classroom {classroom_id!r}; add it with `tacon classroom add` first"
        )
    new_config = ClassesConfig(default=classroom_id, classrooms=config.classrooms)
    _save(new_config, home_dir)
    return new_config


def resolve_db_path(
    *,
    explicit_db: Path | None,
    classroom_id: str | None,
    default_db: Path,
    home_dir: Path | None = None,
) -> Path:
    """Pick a DB path using the documented precedence.

    Precedence (most specific wins):
      1. ``--db <path>`` (``explicit_db``)
      2. ``--classroom <id>`` (looked up in classes.toml)
      3. default classroom from classes.toml (if classes.toml exists)
      4. legacy ``default_db`` (``~/.tacon/tacon.db`` via TACON_HOME)

    Raises :class:`ClassesConfigError` if ``--classroom`` names an
    unknown id, or if classes.toml is malformed.
    """
    if explicit_db is not None:
        return explicit_db

    config = load_classes(home_dir)

    if classroom_id is not None:
        if config is None:
            raise ClassesConfigError(
                f"--classroom {classroom_id!r} given but no classes.toml exists; "
                f"create one with `tacon classroom add {classroom_id} --db PATH`"
            )
        entry = config.get(classroom_id)
        if entry is None:
            known = ", ".join(config.list_ids()) or "(none)"
            raise ClassesConfigError(
                f"unknown classroom {classroom_id!r}; known: {known}"
            )
        return entry.expanded_db_path()

    if config is not None:
        entry = config.get_default()
        if entry is not None:
            return entry.expanded_db_path()

    return default_db
