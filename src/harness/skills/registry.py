"""Filesystem discovery, state, and deferred loading for portable Skills."""

from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable

from src.harness.memory.store import state_root
from src.harness.skills.matcher import EmbeddingFunction, SkillMatcher
from src.harness.skills.models import Skill, SkillProvider
from src.harness.skills.state import SkillStateStore

logger = logging.getLogger(__name__)
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
_BLOCKED_PARTS = {".aws", ".git", ".ssh", "credentials", "secrets", "tokens.json", "token.json", ".env"}
_BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_RESOURCE_LIMIT = 1_048_576


class SkillRegistry(SkillProvider):
    """Manage Skills installed in MiniCC's own directory."""

    def __init__(
        self,
        workspace: Path,
        *,
        skills_root: Path | None = None,
        state_path: Path | None = None,
        state_store: SkillStateStore | None = None,
        embedder: EmbeddingFunction | None = None,
        feature_enabled: bool = True,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.skills_root = (skills_root or state_root() / "skills").expanduser().resolve()
        self.state = state_store or SkillStateStore(state_path)
        self.embedder = embedder
        self.feature_enabled = bool(feature_enabled)
        self._skills: tuple[Skill, ...] | None = None
        self._matcher: SkillMatcher | None = None

    @property
    def enabled(self) -> bool:
        return self.feature_enabled and self.state.enabled()

    def reload(self) -> None:
        self._skills = None
        self._matcher = None

    def all_skills(self) -> list[Skill]:
        skills = self._discover()
        disabled = self.state.disabled()
        return [replace(skill, enabled=self.enabled and skill.id not in disabled) for skill in skills]

    def list(self) -> list[Skill]:
        return [skill for skill in self.all_skills() if skill.enabled]

    def select(self, query: str, *, agent_id: str, limit: int = 5) -> tuple[Skill, ...]:
        del agent_id
        if not self.enabled:
            return ()
        return tuple(self.match(query, top_k=limit))

    def match(self, query: str, *, top_k: int = 5) -> list[Skill]:
        if not self.enabled:
            return []
        if self._matcher is None:
            self._matcher = SkillMatcher(self.list(), self.embedder)
        return self._matcher.match(query, top_k=top_k)

    def load(
        self,
        name: str,
        *,
        resource_path: str | None = None,
        user_initiated: bool = False,
    ) -> Skill | None:
        if not self.enabled:
            return None
        skill = next((item for item in self.all_skills() if item.name == name and item.enabled), None)
        if skill is None or (not user_initiated and not skill.model_invocable):
            return None
        if user_initiated and not skill.user_invocable:
            return None
        if skill.path is None:
            return None
        if resource_path:
            content = self._read_resource(skill, resource_path)
            return replace(skill, content=content) if content is not None else None
        try:
            raw = skill.path.read_text(encoding="utf-8-sig")
            name_from_file, _description, body, _tags, _model_invocable, _user_invocable = _parse_skill(raw, skill.path)
            if name_from_file != skill.name:
                self.reload()
                return None
            return replace(skill, content=body)
        except (OSError, UnicodeError, ValueError) as error:
            logger.warning("Unable to load Skill %s from %s: %s", name, skill.path, error)
            return None

    def enable(self, name: str) -> bool:
        return self._set_skill_enabled(name, True)

    def disable(self, name: str) -> bool:
        return self._set_skill_enabled(name, False)

    def _set_skill_enabled(self, name: str, enabled: bool) -> bool:
        skill = next((item for item in self._discover() if item.name == name), None)
        if skill is None:
            return False
        self.state.set_skill_enabled(skill.id, enabled)
        self.reload()
        return True

    def set_enabled(self, enabled: bool) -> bool:
        if enabled and not self.feature_enabled:
            return False
        self.state.set_enabled(enabled)
        self.reload()
        return True

    def install_package(self, source_id: str, files: dict[str, str]) -> Skill:
        """Atomically install a validated text package into MiniCC's private Skill library."""
        if not self.enabled:
            raise RuntimeError("Skills are disabled.")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 256 or any(
            ord(character) < 32 for character in source_id
        ):
            raise ValueError("Skill source ID is invalid.")
        if not isinstance(files, dict) or not 1 <= len(files) <= 128:
            raise ValueError("Skill package must contain between 1 and 128 files.")

        package: dict[str, str] = {}
        total_bytes = 0
        for file_path, contents in files.items():
            if not isinstance(file_path, str) or not isinstance(contents, str):
                raise ValueError("Skill package files must be UTF-8 text.")
            normalized = file_path.replace("\\", "/")
            parts = normalized.split("/")
            if normalized.startswith("/") or "\\" in file_path or any(
                part in {"", ".", ".."} for part in parts
            ):
                raise ValueError("Skill package path must stay inside the package.")
            if any(part.casefold() in _BLOCKED_PARTS or part.casefold().startswith(".env") for part in parts):
                raise ValueError("Skill package contains a protected path.")
            if Path(normalized).suffix.casefold() in _BLOCKED_SUFFIXES or normalized == ".minicc-source":
                raise ValueError("Skill package contains a protected file.")
            size = len(contents.encode("utf-8"))
            if size > _RESOURCE_LIMIT:
                raise ValueError("Skill package file exceeds 1 MB.")
            total_bytes += size
            if total_bytes > 4 * _RESOURCE_LIMIT:
                raise ValueError("Skill package exceeds 4 MB.")
            if normalized in package:
                raise ValueError("Skill package contains duplicate paths.")
            package[normalized] = contents

        manifest = package.get("SKILL.md")
        if manifest is None:
            raise ValueError("Skill package is missing SKILL.md.")
        name, _description, _body, _tags, _model_invocable, _user_invocable = _parse_skill(
            manifest, Path("SKILL.md"),
        )
        if not _NAME.fullmatch(name):
            raise ValueError("Skill name must be a lowercase slug using letters, numbers, hyphens, or underscores.")
        if any(skill.name == name for skill in self._discover()):
            raise FileExistsError(f'Skill "{name}" is already installed.')

        self.skills_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.skills_root / name
        if destination.exists():
            raise FileExistsError(f'Skill "{name}" is already installed.')
        staging = Path(tempfile.mkdtemp(prefix=".minicc-skill-", dir=self.skills_root.parent))
        try:
            for relative, contents in package.items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_text(contents, encoding="utf-8")
                target.chmod(0o600)
            source_file = staging / ".minicc-source"
            source_file.write_text(f"skillmd.com/{source_id.strip()}\n", encoding="utf-8")
            source_file.chmod(0o600)
            staging.chmod(0o700)
            staging.rename(destination)
        except OSError:
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        self.reload()
        installed = next((skill for skill in self.all_skills() if skill.name == name), None)
        if installed is None:
            raise RuntimeError("Skill was installed but could not be registered.")
        return installed

    def _discover(self) -> tuple[Skill, ...]:
        if self._skills is not None:
            return self._skills
        by_name: dict[str, Skill] = {}
        for path in _skill_files(self.skills_root):
            skill = _load_metadata(path, "minicc")
            if skill is None:
                continue
            by_name.setdefault(skill.name, skill)
        self._skills = tuple(by_name[name] for name in sorted(by_name))
        return self._skills

    @staticmethod
    def _read_resource(skill: Skill, relative_path: str) -> str | None:
        if skill.path is None or not isinstance(relative_path, str) or not relative_path.strip():
            return None
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("Skill resource path must be a relative path inside its package.")
        parts = tuple(normalized.split("/"))
        if any(part.casefold() in _BLOCKED_PARTS or part.casefold().startswith(".env") for part in parts):
            raise ValueError("Skill resource path is protected.")
        if Path(normalized).suffix.casefold() in _BLOCKED_SUFFIXES:
            raise ValueError("Skill resource path is protected.")
        root = skill.path.parent.resolve(strict=True)
        target = (root / normalized).resolve(strict=True)
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError("Skill resource path must stay inside its package.") from error
        if not target.is_file() or target.stat().st_size > _RESOURCE_LIMIT:
            raise ValueError("Skill resource must be a text file no larger than 1 MB.")
        try:
            return target.read_text(encoding="utf-8")[:16_000]
        except UnicodeError as error:
            raise ValueError("Skill resource is not UTF-8 text.") from error


def _skill_files(root: Path) -> tuple[Path, ...]:
    if not root.exists() or not root.is_dir():
        return ()
    found: list[Path] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError as error:
        logger.warning("Unable to discover Skills under %s: %s", root, error)
        return ()
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        manifest = child / "SKILL.md"
        if not manifest.is_symlink() and manifest.is_file():
            found.append(manifest)
    return tuple(found)


def _load_metadata(path: Path, source: str) -> Skill | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        name, description, _body, tags, model_invocable, user_invocable = _parse_skill(raw, path)
    except (OSError, UnicodeError, ValueError) as error:
        logger.warning("Skipping invalid Skill manifest %s: %s", path, error)
        return None
    if not _NAME.fullmatch(name) or len(name) > 64:
        logger.warning("Skipping Skill with invalid name in %s", path)
        return None
    try:
        installed_from = (path.parent / ".minicc-source").read_text(encoding="utf-8").strip()
        if installed_from:
            source = installed_from[:256]
    except OSError:
        pass
    return Skill(
        name, name, None, source, description, tags, path.resolve(), True,
        model_invocable, user_invocable,
    )


def _parse_skill(raw: str, path: Path) -> tuple[str, str, str, tuple[str, ...], bool, bool]:
    lines = raw.splitlines()
    metadata: dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        try:
            body_start = next(
                index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}
            ) + 1
        except StopIteration as error:
            raise ValueError("unterminated frontmatter") from error
        metadata = _parse_yaml_subset(lines[1 : body_start - 1])
    body = "\n".join(lines[body_start:]).strip()
    name = metadata.get("name", "").strip()
    if not name:
        name = path.stem if path.name.casefold() != "skill.md" else path.parent.name
    description = metadata.get("description", "").strip()
    when = metadata.get("when_to_use", metadata.get("whenToUse", "")).strip()
    if when:
        description = f"{description} {when}".strip()
    if not description:
        description = next((line.strip("# ") for line in body.splitlines() if line.strip()), "")[:1024]
    if not description:
        raise ValueError("frontmatter needs a description or a non-empty instruction body")
    tags = _parse_tags(
        metadata.get("tags[]", "")
        or metadata.get("tags", "")
        or metadata.get("metadata.tags[]", "")
        or metadata.get("metadata.tags", "")
    )
    return (
        name,
        description[:1024],
        body,
        tags,
        not _parse_bool(metadata.get("disable-model-invocation"), False),
        _parse_bool(metadata.get("user-invocable"), True),
    )


def _parse_yaml_subset(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    parent = ""
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            match = _FRONTMATTER_KEY.match(line)
            if not match:
                index += 1
                continue
            key, value = match.group(1), (match.group(2) or "").strip()
            parent = key if not value else ""
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                parts: list[str] = []
                index += 1
                while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                    if lines[index].strip():
                        parts.append(_unquote(lines[index].strip()))
                    index += 1
                values[key] = (" " if value.startswith(">") else "\n").join(parts)
                continue
            if value:
                values[key] = _unquote(value)
            index += 1
            continue
        stripped = line.strip()
        if parent and stripped.startswith("-"):
            key = f"{parent}[]"
            values[key] = ",".join(filter(None, (values.get(key, ""), _unquote(stripped[1:].strip()))))
        elif parent:
            match = _FRONTMATTER_KEY.match(stripped)
            if match:
                key, value = match.group(1), (match.group(2) or "").strip()
                full_key = f"{parent}.{key}"
                if not value:
                    parent = full_key
                    index += 1
                    continue
                if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                    parts = []
                    index += 1
                    while index < len(lines) and (
                        not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent
                    ):
                        if lines[index].strip():
                            parts.append(_unquote(lines[index].strip()))
                        index += 1
                    values[full_key] = (" " if value.startswith(">") else "\n").join(parts)
                    continue
                if value:
                    values[full_key] = _unquote(value)
        index += 1
    for key in ("tags", "metadata.tags"):
        list_key = f"{key}[]"
        if list_key in values:
            values[key] = values[list_key]
    return values


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_tags(raw: str) -> tuple[str, ...]:
    value = raw.strip()
    if not value:
        return ()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return tuple(dict.fromkeys(_unquote(item.strip()) for item in value.split(",") if item.strip()))


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = _unquote(value).casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ValueError(f"invalid boolean frontmatter value: {value}")
