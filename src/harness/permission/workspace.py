"""Filesystem boundary for workspace-relative agent operations.

Tools describe an operation; this module owns path validation, protected-path
rules, symlink rejection, bounded reads, scans and atomic file publication.
"""

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileSnapshot:
    text: str
    identity: tuple[int, int]
    digest: str


_MAX_FILE_BYTES = 1_048_576
_MAX_SCAN_FILES = 10_000
_READ_CHUNK = 65_536
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BLOCKED = {
    ".aws", ".git", ".gitmodules", ".hg", ".netrc", ".npmrc", ".pypirc", ".ssh", ".svn",
    "auth.json", "credential", "credentials", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
    "secret", "secrets", "token.json", "tokens.json",
}
_BLOCKED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SKIP_DIRECTORIES = {
    ".cache", ".git", ".hg", ".idea", ".mypy_cache", ".nox", ".pytest_cache", ".ruff_cache",
    ".svn", ".tox", ".venv", ".vscode", "build", "coverage", "dist", "node_modules", "target",
    "vendor", "venv",
}


class Workspace:
    """A resolved workspace root with descriptor-relative file operations."""

    def __init__(self, root: Path) -> None:
        if not _O_NOFOLLOW or os.open not in os.supports_dir_fd:
            raise ValueError("当前平台不支持安全的工作区文件访问，请使用 --chat-only。")
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("工作区目录不可访问。") from error
        if not self.root.is_dir():
            raise ValueError("工作区根目录必须是目录。")

    @staticmethod
    def parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
        if not isinstance(path, str) or "\x00" in path:
            raise WorkspaceError("INVALID_PATH", "路径格式无效。")
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
            raise WorkspaceError("INVALID_PATH", "只能访问工作区内的相对路径。")
        parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
        if not allow_root and not parts:
            raise WorkspaceError("INVALID_PATH", "必须提供工作区内的文件路径。")
        if any(part == ".." for part in parts):
            raise WorkspaceError("INVALID_PATH", "路径不能离开工作区。")
        for part in parts:
            lowered = part.lower()
            if (
                lowered in _BLOCKED
                or lowered.startswith("credential")
                or lowered.startswith("secret")
                or lowered == ".env"
                or lowered.startswith(".env.")
                or lowered.endswith(_BLOCKED_SUFFIXES)
            ):
                raise WorkspaceError("ACCESS_DENIED", "该工作区路径受保护。")
        return parts

    def display(self, parts: tuple[str, ...]) -> str:
        return "/".join(parts) or "."

    def _open_root(self) -> int:
        try:
            return os.open(self.root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        except OSError as error:
            raise self._map_error(error, ".") from error

    def _open_directory(self, parts: tuple[str, ...]) -> int:
        fd = self._open_root()
        try:
            for part in parts:
                child = os.open(
                    part,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=fd,
                )
                os.close(fd)
                fd = child
            return fd
        except OSError as error:
            os.close(fd)
            raise self._map_error(error, self.display(parts)) from error
        except BaseException:
            os.close(fd)
            raise

    def _open_parent(self, parts: tuple[str, ...]) -> tuple[int, str]:
        if not parts:
            raise WorkspaceError("INVALID_PATH", "需要文件路径。")
        return self._open_directory(parts[:-1]), parts[-1]

    @staticmethod
    def _map_error(error: OSError, display: str) -> WorkspaceError:
        if isinstance(error, FileNotFoundError):
            return WorkspaceError("NOT_FOUND", f"工作区内找不到：{display}")
        if isinstance(error, PermissionError):
            return WorkspaceError("ACCESS_DENIED", f"无法访问工作区路径：{display}")
        if isinstance(error, (NotADirectoryError, IsADirectoryError)):
            return WorkspaceError("INVALID_TARGET", f"工作区目标不是有效文件：{display}")
        if getattr(error, "errno", None) == errno.ELOOP:
            return WorkspaceError("ACCESS_DENIED", "工作区工具不跟随符号链接。")
        return WorkspaceError("IO_ERROR", f"无法访问工作区路径：{display}")

    def snapshot(self, path: str) -> FileSnapshot:
        parts = self.parts(path)
        parent, leaf = self._open_parent(parts)
        try:
            return self._snapshot_at(parent, leaf, self.display(parts))
        finally:
            os.close(parent)

    def _snapshot_at(self, parent: int, leaf: str, display: str | None = None) -> FileSnapshot:
        fd = -1
        try:
            fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=parent)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceError("INVALID_TARGET", "工作区工具只读取普通文件。")
            if metadata.st_size > _MAX_FILE_BYTES:
                raise WorkspaceError("TOO_LARGE", f"文件超过 {_MAX_FILE_BYTES} 字节上限。")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise WorkspaceError("TOO_LARGE", f"文件超过 {_MAX_FILE_BYTES} 字节上限。")
                chunks.append(chunk)
            try:
                data = b"".join(chunks)
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkspaceError("NOT_TEXT", "文件不是 UTF-8 文本。") from error
            return FileSnapshot(text, (metadata.st_dev, metadata.st_ino), hashlib.sha256(data).hexdigest())
        except OSError as error:
            raise self._map_error(error, display or leaf) from error
        finally:
            if fd >= 0:
                os.close(fd)

    def is_file(self, path: str) -> bool:
        parts = self.parts(path)
        parent, leaf = self._open_parent(parts)
        try:
            metadata = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            raise self._map_error(error, self.display(parts)) from error
        finally:
            os.close(parent)
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceError("ACCESS_DENIED", "工作区工具不跟随符号链接。")
        return stat.S_ISREG(metadata.st_mode)

    def create(self, path: str, content: str, checkpoint: Callable[[], None]) -> None:
        parts = self.parts(path)
        parent, leaf = self._open_parent(parts)
        try:
            self._publish(parent, leaf, content, checkpoint, replace=False)
        finally:
            os.close(parent)

    def replace(
        self,
        path: str,
        content: str,
        expected_identity: tuple[int, int],
        expected_digest: str,
        checkpoint: Callable[[], None],
    ) -> None:
        parts = self.parts(path)
        parent, leaf = self._open_parent(parts)
        try:
            current = self.snapshot(path)
            if current.identity != expected_identity or current.digest != expected_digest:
                raise WorkspaceError("FILE_CHANGED", "文件在审批后发生变化，已取消写入。")
            self._publish(
                parent, leaf, content, checkpoint, replace=True,
                expected_identity=expected_identity, expected_digest=expected_digest,
            )
        finally:
            os.close(parent)

    def _publish(
        self,
        parent: int,
        leaf: str,
        content: str,
        checkpoint: Callable[[], None],
        *,
        replace: bool,
        expected_identity: tuple[int, int] | None = None,
        expected_digest: str | None = None,
    ) -> None:
        data = content.encode("utf-8")
        if len(data) > _MAX_FILE_BYTES:
            raise WorkspaceError("TOO_LARGE", f"文件超过 {_MAX_FILE_BYTES} 字节上限。")
        temporary = f".minicc-{secrets.token_hex(12)}.tmp"
        fd = -1
        preserve_temporary = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            view = memoryview(data)
            while view:
                checkpoint()
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            staged_metadata = os.fstat(fd)
            staged_identity = (staged_metadata.st_dev, staged_metadata.st_ino)
            staged_digest = hashlib.sha256(data).hexdigest()
            os.close(fd)
            fd = -1
            checkpoint()
            if replace:
                _exchange(parent, temporary, leaf)
                try:
                    previous = self._snapshot_at(parent, temporary)
                except WorkspaceError:
                    previous = None
                if (
                    previous is None
                    or previous.identity != expected_identity
                    or previous.digest != expected_digest
                ):
                    try:
                        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == staged_identity:
                            try:
                                _exchange(parent, temporary, leaf)
                                rolled_back = self._snapshot_at(parent, temporary)
                                if (
                                    rolled_back.identity != staged_identity
                                    or rolled_back.digest != staged_digest
                                ):
                                    preserve_temporary = True
                            except (OSError, WorkspaceError):
                                preserve_temporary = True
                        else:
                            preserve_temporary = True
                    except (OSError, WorkspaceError):
                        preserve_temporary = True
                    detail = f"文件在审批后发生变化，已取消写入。"
                    if preserve_temporary:
                        detail += f" 原文件副本保留在 {temporary}。"
                    raise WorkspaceError("FILE_CHANGED", detail)
            else:
                os.link(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(temporary, dir_fd=parent)
        except FileExistsError as error:
            raise WorkspaceError("ALREADY_EXISTS", "目标文件已存在。") from error
        except OSError as error:
            raise self._map_error(error, leaf) from error
        finally:
            if fd >= 0:
                os.close(fd)
            if not preserve_temporary:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass

    def files(
        self,
        base: str = "",
        checkpoint: Callable[[], None] = lambda: None,
    ) -> Iterator[str]:
        root_parts = self.parts(base, allow_root=True)
        queue = [root_parts]
        scanned = 0
        while queue and scanned < _MAX_SCAN_FILES:
            checkpoint()
            directory = queue.pop(0)
            fd = self._open_directory(directory)
            try:
                names = sorted(os.listdir(fd))
                for name in names:
                    checkpoint()
                    scanned += 1
                    if scanned > _MAX_SCAN_FILES:
                        break
                    child = (*directory, name)
                    try:
                        self.parts(self.display(child))
                        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        continue
                    except WorkspaceError:
                        continue
                    mode = metadata.st_mode
                    if stat.S_ISDIR(mode):
                        if name not in _SKIP_DIRECTORIES:
                            queue.append(child)
                    elif stat.S_ISREG(mode):
                        yield self.display(child)
            finally:
                os.close(fd)


def _exchange(parent: int, left: str, right: str) -> None:
    """Atomically swap two directory entries on supported local platforms."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            rename = libc.renameatx_np
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
        else:
            raise WorkspaceError("ATOMIC_REPLACE_UNAVAILABLE", "当前平台不支持安全的原子文件交换。")
    except AttributeError as error:
        raise WorkspaceError("ATOMIC_REPLACE_UNAVAILABLE", "当前系统不支持安全的原子文件交换。") from error
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(left), parent, os.fsencode(right), 0x00000002) != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
            raise WorkspaceError("ATOMIC_REPLACE_UNAVAILABLE", "当前文件系统不支持安全的原子文件交换。")
        raise OSError(error_number, os.strerror(error_number), right)
