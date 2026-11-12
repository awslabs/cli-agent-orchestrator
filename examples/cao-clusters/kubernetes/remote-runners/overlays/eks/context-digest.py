"""Print a short digest of the files the EKS image is built from.

deploy.sh tags the image with it, so the same content always gets the same
tag in the immutable repository and any change gets a new one. The paths are
the ones examples/cao-clusters/kubernetes/eks/Dockerfile copies (see the
repository's .dockerignore); __pycache__ and *.pyc are left out, as there.

Usage: python3 context-digest.py <repository root>
"""

import hashlib
import os
import stat
import sys

PATHS = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "NOTICE",
    "scripts/hatch_build_tui_tag.py",
    "src",
    "examples/cao-clusters/kubernetes/eks",
    "test/providers/fixtures/bin/mock_cli",
)


def files(top):
    if os.path.isdir(top) and not os.path.islink(top):
        for parent, dirs, names in os.walk(top):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(
                names + [d for d in dirs if os.path.islink(os.path.join(parent, d))]
            ):
                if not name.endswith(".pyc"):
                    yield os.path.join(parent, name)
    else:
        yield top  # a missing path raises in lstat below


def main(root):
    os.chdir(root)
    digest = hashlib.sha256()
    for top in PATHS:
        for path in files(top):
            mode = os.lstat(path).st_mode
            digest.update(path.encode() + b"\0")
            if stat.S_ISLNK(mode):
                digest.update(b"link\0" + os.readlink(path).encode())
            else:
                digest.update(b"exec\0" if mode & 0o111 else b"file\0")
                with open(path, "rb") as handle:
                    digest.update(hashlib.sha256(handle.read()).digest())
            digest.update(b"\0")
    print(digest.hexdigest()[:12])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: context-digest.py <repository root>")
    main(sys.argv[1])
