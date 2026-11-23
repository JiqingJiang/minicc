"""Persistent records and lifecycle transitions for one memory scope."""

from __future__ import annotations

import json
import os
import re
import uuid
import fcntl
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from src.harness.memory.tokenization import tokenize

MEMORY_CATEGORIES = {
    "user_preference", "user_correction", "project_fact", "decision", "procedure", "reference",
}
MEMORY_SOURCES = {
    "user_instruction", "user_correction", "confirmed_decision", "agent_extraction", "user_edit",
}
MEMORY_RELATIONS = {"new", "duplicate", "supplement", "supersede", "time_update", "conflict", "uncertain"}
SENSITIVE = re.compile(
    r"(?i)(?:api[_ -]?key|secret|password|passwd|token|credential|private[_ -]?key)\s*[:=]\s*\S+"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16})\b"
)


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "minicc"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialized(method):
    """Serialize read-modify-write operations across threads and processes."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        _private_directory(self.path.parent)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return method(self, *args, **kwargs)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return wrapped


@dataclass
class Memory:
    id: str
    content: str
    category: str
    scope: str
    source_type: str
    source_ref: str | None
    created_at: str
    updated_at: str
    active: bool = True
    history: list[dict] = field(default_factory=list)
    status: str = "active"
    effective_at: str | None = None
    valid_until: str | None = None
    keywords: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)
    conflict_group: str | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    sources: list[dict] = field(default_factory=list)


class MemoryStore:
    """JSON-backed store for exactly one global or project namespace."""

    def __init__(self, path: Path, scope: str):
        if scope not in {"global", "project"}:
            raise ValueError("记忆范围必须是 global 或 project。")
        self.path, self.scope = path, scope

    def list(self, *, include_history: bool = False, include_deleted: bool = False) -> list[Memory]:
        items = self._read_all()
        visible = []
        for item in items:
            if not item.active or item.status == "deleted":
                if include_deleted:
                    visible.append(item)
                continue
            if item.status == "superseded" and not include_history:
                continue
            visible.append(item)
        return visible

    def get(self, memory_id: str, *, include_deleted: bool = False) -> Memory:
        items = self._read_all()
        item = self._resolve(items, memory_id)
        if (not item.active or item.status == "deleted") and not include_deleted:
            raise KeyError("没有找到这条有效记忆。")
        return item

    @_serialized
    def remember(
        self,
        content: str,
        *,
        category: str,
        source_type: str,
        source_ref: str | None,
        relation: str = "new",
        related_ids: list[str] | None = None,
        effective_at: str | None = None,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> tuple[Memory, str]:
        content = content.strip()
        if len(content) < 2 or len(content) > 1800:
            raise ValueError("记忆内容长度必须在 2 到 1800 个字符之间。")
        if SENSITIVE.search(content):
            raise ValueError("内容疑似包含密钥或凭证，未保存。")
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"不支持的记忆类别：{category}")
        if source_type not in MEMORY_SOURCES - {"user_edit"}:
            raise ValueError(f"不支持的记忆来源：{source_type}")
        if relation not in MEMORY_RELATIONS:
            raise ValueError(f"不支持的记忆关系：{relation}")

        all_items = self._read_all()
        duplicate = next((
            item for item in all_items
            if item.active and item.status in {"active", "conflict"}
            and item.category == category and _normalize(item.content) == _normalize(content)
        ), None)
        if duplicate is not None:
            self._add_source(duplicate, source_type, source_ref)
            duplicate.updated_at = now_utc()
            self._write(all_items)
            return duplicate, "duplicate"

        related = self._resolve_related(all_items, related_ids or [])
        required_relations = {"duplicate", "supplement", "supersede", "time_update", "conflict", "uncertain"}
        if relation in required_relations and not related:
            raise ValueError(f"{relation} 操作需要提供相关记忆 ID。")
        if relation == "duplicate" and len(related) != 1:
            raise ValueError("duplicate 操作必须且只能指定一条相关记忆。")
        if relation == "new" and related:
            raise ValueError("new 操作不能带相关记忆 ID；请说明它们是补充、取代还是冲突。")
        if any(item.category != category for item in related) and relation in {"duplicate", "supersede", "time_update"}:
            raise ValueError("重复或取代关系必须是同一记忆类别。")
        if relation == "supersede" and source_type not in {"user_instruction", "user_correction", "confirmed_decision"}:
            raise ValueError("只有用户明确指示、纠正或确认的决定才能取代长期记忆。")
        if relation == "supersede" and not _trusted_source(source_ref, source_type):
            raise ValueError("取代记忆需要来自当前用户明确指示或纠正的可信来源。")
        if relation == "duplicate":
            target = related[0]
            self._add_source(target, source_type, source_ref)
            target.updated_at = now_utc()
            self._write(all_items)
            return target, "duplicate"

        now = now_utc()
        effective = effective_at or now
        new_item = Memory(
            id=uuid.uuid4().hex,
            content=content,
            category=category,
            scope=self.scope,
            source_type=source_type,
            source_ref=source_ref,
            created_at=now,
            updated_at=now,
            status="conflict" if relation in {"conflict", "uncertain"} else "active",
            effective_at=effective,
            keywords=_keywords(content),
            entities=_entities(content),
            related=[{"relation": relation, "memory_id": item.id} for item in related],
            embedding=embedding,
            embedding_model=embedding_model,
        )
        self._add_source(new_item, source_type, source_ref)

        if relation in {"supersede", "time_update"}:
            for old in related:
                self._add_source(old, source_type, source_ref)
                old.status = "superseded"
                old.valid_until = effective
                old.updated_at = now
                old.related.append({"relation": "superseded_by", "memory_id": new_item.id})
        elif relation in {"conflict", "uncertain"}:
            groups = {item.conflict_group for item in related if item.conflict_group}
            if len(groups) > 1:
                raise ValueError("相关记忆属于不同冲突组，不能合并为一个冲突。")
            group = next(iter(groups), uuid.uuid4().hex)
            new_item.conflict_group = group
            for old in related:
                self._add_source(old, source_type, source_ref)
                old.status = "conflict"
                old.conflict_group = group
                old.updated_at = now
        else:
            for old in related:
                self._add_source(old, source_type, source_ref)
                old.related.append({"relation": relation, "memory_id": new_item.id})

        all_items.append(new_item)
        self._write(all_items)
        return new_item, relation

    @_serialized
    def update(self, memory_id: str, content: str, *, embedding: list[float] | None = None, embedding_model: str | None = None) -> Memory:
        content = content.strip()
        if len(content) < 2 or len(content) > 1800 or SENSITIVE.search(content):
            raise ValueError("记忆内容为空、过长或疑似包含密钥，未保存。")
        items = self._read_all()
        item = self._resolve(items, memory_id)
        if not item.active or item.status == "deleted":
            raise KeyError("不能编辑已删除的记忆。")
        item.history.append(_revision(item))
        item.content = content
        item.keywords = _keywords(content)
        item.entities = _entities(content)
        item.source_type = "user_edit"
        item.source_ref = None
        item.updated_at = now_utc()
        self._add_source(item, "user_edit", None)
        item.embedding, item.embedding_model = embedding, embedding_model
        self._write(items)
        return item

    @_serialized
    def set_embeddings(self, embeddings: dict[str, tuple[list[float], str]]) -> None:
        if not embeddings:
            return
        items = self._read_all()
        changed = False
        for item in items:
            value = embeddings.get(item.id)
            if value is not None and item.active and item.status != "deleted":
                item.embedding, item.embedding_model = value
                changed = True
        if changed:
            self._write(items)

    @_serialized
    def resolve(self, memory_id: str) -> Memory:
        items = self._read_all()
        chosen = self._resolve(items, memory_id)
        if chosen.status != "conflict" or not chosen.conflict_group:
            raise ValueError("这条记忆不属于未解决的冲突。")
        now = now_utc()
        group = chosen.conflict_group
        chosen.status, chosen.conflict_group, chosen.updated_at = "active", None, now
        resolution_ref = json.dumps({"action": "resolve_conflict", "kept": chosen.id}, ensure_ascii=False)
        self._add_source(chosen, "user_instruction", resolution_ref)
        for other in items:
            if other.id == chosen.id or other.conflict_group != group:
                continue
            other.status = "superseded"
            other.valid_until = chosen.effective_at or now
            other.conflict_group = None
            other.updated_at = now
            other.related.append({"relation": "resolved_by_user", "memory_id": chosen.id})
            self._add_source(other, "user_instruction", resolution_ref)
            chosen.related.append({"relation": "resolved", "memory_id": other.id})
        self._write(items)
        return chosen

    @_serialized
    def delete(self, memory_id: str) -> None:
        items = self._read_all()
        item = self._resolve(items, memory_id)
        group = item.conflict_group
        items = [candidate for candidate in items if candidate.id != item.id]
        for candidate in items:
            candidate.related = [link for link in candidate.related if link.get("memory_id") != item.id]
            if group and candidate.conflict_group == group:
                peers = [peer for peer in items if peer.conflict_group == group and peer.active]
                if len(peers) < 2:
                    for peer in peers:
                        peer.status, peer.conflict_group = "active", None
        self._write(items)

    def history(self, memory_id: str) -> Memory:
        return self.get(memory_id, include_deleted=True)

    def conflicts(self) -> list[Memory]:
        return [item for item in self.list() if item.status == "conflict"]

    def _read_all(self) -> list[Memory]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"无法读取记忆文件 {self.path}: {error}") from error
        if not isinstance(raw, list):
            raise RuntimeError(f"记忆文件格式无效：{self.path}")
        allowed = {field.name for field in fields(Memory)}
        try:
            items = [Memory(**_migrate_record(item, allowed)) for item in raw]
        except (AttributeError, TypeError, KeyError) as error:
            raise RuntimeError(f"记忆文件记录格式无效：{self.path}") from error
        # Older MiniCC versions used only ``active`` for soft deletion.
        for item in items:
            if not item.active and item.status == "active":
                item.status = "deleted"
        return items

    def _resolve_related(self, items: list[Memory], ids: list[str]) -> list[Memory]:
        if len(ids) > 5 or len(set(ids)) != len(ids):
            raise ValueError("相关记忆 ID 最多 5 个且不能重复。")
        resolved = [self._resolve(items, identity) for identity in ids]
        if any(not item.active or item.status not in {"active", "conflict"} for item in resolved):
            raise ValueError("关联记忆必须是当前有效记录。")
        return resolved

    @staticmethod
    def _resolve(items: list[Memory], memory_id: str) -> Memory:
        if not memory_id:
            raise KeyError("请提供记忆 ID。")
        matches = [item for item in items if item.id.startswith(memory_id)]
        if not matches:
            raise KeyError("没有找到这条记忆。")
        if len(matches) > 1:
            raise ValueError("ID 前缀不唯一，请输入更长的 ID。")
        return matches[0]

    @staticmethod
    def _add_source(item: Memory, source_type: str, source_ref: str | None) -> None:
        source = {"source_type": source_type, "source_ref": source_ref, "observed_at": now_utc()}
        if source not in item.sources:
            item.sources.append(source)

    def _write(self, items: list[Memory]) -> None:
        _private_directory(self.path.parent)
        fd, temporary_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _trusted_source(source_ref: str | None, source_type: str) -> bool:
    if not source_ref:
        return False
    try:
        source = json.loads(source_ref)
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(source.get("session_id") and source.get("turn_id") and source.get("trusted_source_type") == source_type)


def _normalize(content: str) -> str:
    return " ".join(content.casefold().split())


def _migrate_record(raw: object, allowed: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("memory record must be an object")
    item = {key: value for key, value in raw.items() if key in allowed}
    legacy_category = {"preference": "user_preference", "correction": "user_correction", "fact": "project_fact"}
    item["category"] = legacy_category.get(item.get("category"), item.get("category"))
    item["scope"] = {"local": "project", "user": "global"}.get(item.get("scope"), item.get("scope"))
    stamp = raw.get("date") or raw.get("created_at") or now_utc()
    item["created_at"] = item.get("created_at") or stamp
    item["updated_at"] = item.get("updated_at") or raw.get("date") or item["created_at"]
    item.setdefault("source_ref", raw.get("source") if isinstance(raw.get("source"), str) else None)
    item.setdefault("source_type", "agent_extraction")
    item.setdefault("active", True)
    if not isinstance(item.get("history"), list):
        item["history"] = []
    if not isinstance(item.get("sources"), list):
        item["sources"] = []
    return item


def _keywords(content: str) -> list[str]:
    return list(dict.fromkeys(tokenize(content)))[:80]


def _entities(content: str) -> list[str]:
    # Retain likely identifiers and proper-name candidates as retrieval hints.
    matches = re.findall(r"(?:[A-Z][A-Za-z0-9_.-]{1,}|[\w.-]+/[\w./-]+|#[\w-]+)", content)
    return list(dict.fromkeys(matches))[:20]


def _revision(item: Memory) -> dict:
    return {
        "content": item.content,
        "source_type": item.source_type,
        "source_ref": item.source_ref,
        "updated_at": item.updated_at,
        "sources": list(item.sources),
        "status": item.status,
        "effective_at": item.effective_at,
        "valid_until": item.valid_until,
    }


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
