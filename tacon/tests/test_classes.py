"""Unit tests for tacon.classes — classes.toml load/save/resolve."""

from __future__ import annotations

from pathlib import Path

import pytest

from tacon.classes import (
    ClassesConfigError,
    Classroom,
    add_classroom,
    load_classes,
    resolve_db_path,
    set_default,
)

# ---------- load_classes ----------


def test_load_classes_returns_none_when_file_absent(tmp_path: Path) -> None:
    """Absent classes.toml = legacy single-DB mode. None signals that."""
    assert load_classes(home_dir=tmp_path) is None


def test_load_classes_parses_valid_file(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text(
        'default = "cs101-spring"\n\n'
        "[classrooms.cs101-spring]\n"
        'db_path = "~/.tacon/cs101-spring.db"\n'
        'description = "CS101 Spring 2026"\n\n'
        "[classrooms.cs101-fall]\n"
        'db_path = "~/.tacon/cs101-fall.db"\n',
        encoding="utf-8",
    )
    config = load_classes(home_dir=tmp_path)
    assert config is not None
    assert config.default == "cs101-spring"
    assert set(config.list_ids()) == {"cs101-spring", "cs101-fall"}
    spring = config.get("cs101-spring")
    assert spring is not None
    assert spring.db_path == "~/.tacon/cs101-spring.db"
    assert spring.description == "CS101 Spring 2026"
    fall = config.get("cs101-fall")
    assert fall is not None
    assert fall.description == ""


def test_load_classes_invalid_toml_raises(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text("this is = not [ valid toml", encoding="utf-8")
    with pytest.raises(ClassesConfigError, match="invalid TOML"):
        load_classes(home_dir=tmp_path)


def test_load_classes_rejects_non_string_default(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text(
        "default = 42\n[classrooms.x]\ndb_path = \"a.db\"\n", encoding="utf-8"
    )
    with pytest.raises(ClassesConfigError, match="'default' must be a string"):
        load_classes(home_dir=tmp_path)


def test_load_classes_rejects_default_pointing_at_unknown_id(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text(
        'default = "nope"\n[classrooms.real]\ndb_path = "a.db"\n', encoding="utf-8"
    )
    with pytest.raises(ClassesConfigError, match="no \\[classrooms.nope\\]"):
        load_classes(home_dir=tmp_path)


def test_load_classes_rejects_missing_db_path(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text(
        '[classrooms.x]\ndescription = "no db path here"\n', encoding="utf-8"
    )
    with pytest.raises(ClassesConfigError, match="db_path is required"):
        load_classes(home_dir=tmp_path)


def test_load_classes_rejects_non_table_classrooms(tmp_path: Path) -> None:
    (tmp_path / "classes.toml").write_text(
        'classrooms = "this should be a table"\n', encoding="utf-8"
    )
    with pytest.raises(ClassesConfigError, match="must be a table"):
        load_classes(home_dir=tmp_path)


# ---------- add_classroom ----------


def test_add_classroom_creates_file_and_sets_first_default(tmp_path: Path) -> None:
    config = add_classroom(
        classroom_id="cs101-spring",
        db_path="~/.tacon/cs101-spring.db",
        description="CS101 Spring",
        home_dir=tmp_path,
    )
    assert config.default == "cs101-spring"
    assert (tmp_path / "classes.toml").exists()
    # Round-trip via load_classes
    reloaded = load_classes(home_dir=tmp_path)
    assert reloaded is not None
    assert reloaded.default == "cs101-spring"
    assert reloaded.get("cs101-spring") == Classroom(
        id="cs101-spring",
        db_path="~/.tacon/cs101-spring.db",
        description="CS101 Spring",
    )


def test_add_classroom_second_does_not_overwrite_default(tmp_path: Path) -> None:
    add_classroom(
        classroom_id="cs101-spring", db_path="a.db", home_dir=tmp_path
    )
    config = add_classroom(
        classroom_id="cs101-fall", db_path="b.db", home_dir=tmp_path
    )
    assert config.default == "cs101-spring"  # not overwritten
    assert set(config.list_ids()) == {"cs101-spring", "cs101-fall"}


def test_add_classroom_with_make_default_changes_default(tmp_path: Path) -> None:
    add_classroom(
        classroom_id="cs101-spring", db_path="a.db", home_dir=tmp_path
    )
    config = add_classroom(
        classroom_id="cs101-fall",
        db_path="b.db",
        make_default=True,
        home_dir=tmp_path,
    )
    assert config.default == "cs101-fall"


def test_add_classroom_re_add_overwrites_db_path(tmp_path: Path) -> None:
    add_classroom(
        classroom_id="cs101-spring", db_path="a.db", home_dir=tmp_path
    )
    config = add_classroom(
        classroom_id="cs101-spring",
        db_path="newpath.db",
        description="updated",
        home_dir=tmp_path,
    )
    entry = config.get("cs101-spring")
    assert entry is not None
    assert entry.db_path == "newpath.db"
    assert entry.description == "updated"


# ---------- set_default ----------


def test_set_default_changes_pointer(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101-spring", db_path="a.db", home_dir=tmp_path)
    add_classroom(classroom_id="cs101-fall", db_path="b.db", home_dir=tmp_path)
    config = set_default("cs101-fall", home_dir=tmp_path)
    assert config.default == "cs101-fall"
    # Round-trip
    reloaded = load_classes(home_dir=tmp_path)
    assert reloaded is not None
    assert reloaded.default == "cs101-fall"


def test_set_default_unknown_id_raises(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101-spring", db_path="a.db", home_dir=tmp_path)
    with pytest.raises(ClassesConfigError, match="unknown classroom"):
        set_default("nope", home_dir=tmp_path)


def test_set_default_no_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ClassesConfigError):
        set_default("anything", home_dir=tmp_path)


# ---------- resolve_db_path ----------


def test_resolve_explicit_db_beats_classroom(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101", db_path="from-classroom.db", home_dir=tmp_path)
    explicit = tmp_path / "explicit.db"
    resolved = resolve_db_path(
        explicit_db=explicit,
        classroom_id="cs101",
        default_db=Path("/legacy/default.db"),
        home_dir=tmp_path,
    )
    assert resolved == explicit


def test_resolve_classroom_beats_default(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101", db_path="/path/to/cs101.db", home_dir=tmp_path)
    resolved = resolve_db_path(
        explicit_db=None,
        classroom_id="cs101",
        default_db=Path("/legacy/default.db"),
        home_dir=tmp_path,
    )
    assert resolved == Path("/path/to/cs101.db")


def test_resolve_classroom_expands_tilde(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101", db_path="~/.tacon/cs101.db", home_dir=tmp_path)
    resolved = resolve_db_path(
        explicit_db=None,
        classroom_id="cs101",
        default_db=Path("/legacy/default.db"),
        home_dir=tmp_path,
    )
    assert "~" not in str(resolved)
    assert resolved == (Path.home() / ".tacon" / "cs101.db")


def test_resolve_default_classroom_used_when_flag_omitted(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101", db_path="/cs101.db", home_dir=tmp_path)
    resolved = resolve_db_path(
        explicit_db=None,
        classroom_id=None,
        default_db=Path("/legacy/default.db"),
        home_dir=tmp_path,
    )
    assert resolved == Path("/cs101.db")


def test_resolve_legacy_default_when_no_config(tmp_path: Path) -> None:
    """No classes.toml -> legacy ~/.tacon/tacon.db default returned."""
    legacy = tmp_path / "legacy.db"
    resolved = resolve_db_path(
        explicit_db=None,
        classroom_id=None,
        default_db=legacy,
        home_dir=tmp_path,
    )
    assert resolved == legacy


def test_resolve_unknown_classroom_raises(tmp_path: Path) -> None:
    add_classroom(classroom_id="cs101", db_path="a.db", home_dir=tmp_path)
    with pytest.raises(ClassesConfigError, match="unknown classroom 'nope'"):
        resolve_db_path(
            explicit_db=None,
            classroom_id="nope",
            default_db=Path("/legacy"),
            home_dir=tmp_path,
        )


def test_resolve_classroom_without_config_raises(tmp_path: Path) -> None:
    """--classroom given but no classes.toml exists — user error worth flagging."""
    with pytest.raises(ClassesConfigError, match="no classes.toml exists"):
        resolve_db_path(
            explicit_db=None,
            classroom_id="cs101",
            default_db=Path("/legacy"),
            home_dir=tmp_path,
        )
