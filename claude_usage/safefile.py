"""Descriptor-validated reads for regular, read-only data files.

Some inputs intentionally allow symlinks: people commonly keep dotfiles in a
repository, and a pricing override may live anywhere the user chooses. Others,
such as transcripts and a bearer-token recovery file, require a single-link
path that cannot redirect. Both need the decision made on the object actually
opened: a path check followed by ``open`` can be swapped to a FIFO and block the
process forever.

This module opens first with every portable nonblocking/inheritance guard, then
validates that same descriptor. It does not provide write safety; writers have
additional atomic-replacement requirements.
"""

import os
import stat

_CAN_OPEN_BENEATH_ROOT = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
)


def _open_beneath_root(path, root, flags):
    """Open within a selected root, refusing links in its descendants."""
    root = os.path.abspath(root)
    relative = os.path.relpath(os.path.abspath(path), root)
    parts = relative.split(os.sep)
    if relative == os.curdir or os.pardir in parts or os.path.isabs(relative):
        raise ValueError("file is outside its selected root")
    if not _CAN_OPEN_BENEATH_ROOT:
        # Platforms without descriptor-relative opens retain a best-effort
        # ancestor check. The final descriptor is still validated below.
        parent = root
        for part in parts[:-1]:
            parent = os.path.join(parent, part)
            info = os.lstat(parent)
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ValueError("linked or non-directory transcript ancestor")
        return os.open(path, flags)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NONBLOCK", 0)
    # The user explicitly selected this root, so its own symlink is allowed.
    directory = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def open_regular_file_descriptor(path, *, follow_symlinks=True,
                                 single_link=False, owner_only=False, root=None):
    """Open and validate one regular file, returning its descriptor or ``None``.

    The caller owns a returned descriptor. When symlinks are refused, the
    opened descriptor is cross-checked against ``lstat(path)`` as well as using
    ``O_NOFOLLOW`` where the platform provides it. The identity check also
    catches a path replaced between ``open`` and validation.
    ``root`` confines transcript opens below an explicitly selected directory;
    descriptor-relative traversal is used where the platform supports it.
    """
    if root is not None:
        follow_symlinks = False
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        opened_path = os.fspath(path)
        descriptor = (os.open(opened_path, flags) if root is None else
                      _open_beneath_root(opened_path, root, flags))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None
        if single_link and opened.st_nlink != 1:
            return None
        if (owner_only and os.name == "posix"
                and opened.st_uid != os.getuid()):
            return None
        if not follow_symlinks:
            current = os.lstat(opened_path)
            if (not stat.S_ISREG(current.st_mode)
                    or (single_link and current.st_nlink != 1)
                    or (opened.st_dev, opened.st_ino)
                       != (current.st_dev, current.st_ino)):
                return None
        result = descriptor
        descriptor = None
        return result
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def read_bounded_regular_file(path, max_bytes, *, owner_only=False,
                              follow_symlinks=True, single_link=False):
    """Return at most ``max_bytes`` from a regular file, or ``None``.

    The opened object itself must be regular. Callers choose whether links are
    supported and whether POSIX ownership is required; defaults retain the
    permissive read-only behavior used by managed dotfiles and rate overrides.
    Every failure is represented by ``None``; callers parse the returned bytes
    according to their own format.
    """
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 0):
        return None
    descriptor = open_regular_file_descriptor(
        path,
        follow_symlinks=follow_symlinks,
        single_link=single_link,
        owner_only=owner_only,
    )
    if descriptor is None:
        return None
    try:
        info = os.fstat(descriptor)
        if info.st_size > max_bytes:
            return None

        remaining = max_bytes + 1
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        return raw if len(raw) <= max_bytes else None
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
