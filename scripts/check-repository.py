"""只读检查待发布文件、相对文档链接和可选 Git 历史；不输出命中的凭据。"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
)
PRIVATE_DIRS = {".ssh", ".git-migration", "archives", "memory", "decrypted", "xwechat_files"}
PRIVATE_NAMES = {"config.json", "keys.json", "all_keys.json", "id_rsa", "id_ed25519"}
PRIVATE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".db", ".db-wal", ".db-shm", ".sqlite",
    ".sqlite3", ".sqlite3-journal", ".sqlite3-wal", ".sqlite3-shm", ".dat",
)
LINK = re.compile(r"!?\[[^\]\n]+\]\((?:<([^>]+)>|([^\s)]+))(?:\s+\"[^\"]*\")?\)")


def git(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if result.returncode:
        raise RuntimeError("Git read failed: " + args[0])
    return result.stdout


def private_path(name: str) -> bool:
    path = PurePosixPath(name.lower())
    base = path.name
    return (
        bool(PRIVATE_DIRS.intersection(path.parts[:-1]))
        or (base != ".env.example" and (base == ".env" or base.startswith(".env.")))
        or base in PRIVATE_NAMES
        or base.endswith("_ed25519")
        or base.endswith(PRIVATE_SUFFIXES)
    )


def credential_lines(data: bytes) -> set[int]:
    return {
        data.count(b"\n", 0, match.start()) + 1
        for pattern in SECRET_PATTERNS for match in pattern.finditer(data)
    }


def document_links(name: str, data: bytes) -> list[str]:
    """只核对当前 Markdown 的本地文件目标；不访问网址，不核对标题锚点。"""
    issues = []
    fence = ""
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        line = re.sub(r"`+[^`\n]*`+", "", line)
        for match in LINK.finditer(line):
            target = match.group(1) or match.group(2)
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            relative = unquote(url.path)
            resolved = (ROOT / name).parent.joinpath(relative).resolve()
            if not resolved.is_relative_to(ROOT):
                issues.append(f"{name}:{number}: document link leaves repository")
            elif not resolved.exists():
                issues.append(f"{name}:{number}: missing document target")
    return issues


def current_files() -> list[str]:
    raw = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({name.decode("utf-8") for name in raw.split(b"\0") if name})


def history_blobs() -> tuple[dict[str, set[str]], list[str], int]:
    objects: dict[str, set[str]] = {}
    issues = set()
    commits = git("rev-list", "--all").decode("ascii").splitlines()
    for commit in commits:
        for row in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if not row:
                continue
            metadata, raw_name = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8")
            if private_path(name):
                issues.add(f"history:{name}: private data path")
            if mode == "120000":
                issues.add(f"history:{name}: symlink requires manual review")
            if kind == "blob":
                objects.setdefault(oid, set()).add(name)
    return objects, sorted(issues), len(commits)


def scan_history(objects: dict[str, set[str]]) -> list[str]:
    if not objects:
        return []
    ids = sorted(objects)
    # Batch mode avoids one process per blob; all bytes remain local and in memory.
    result = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=ROOT,
        input=("\n".join(ids) + "\n").encode("ascii"), capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("Git read failed: cat-file")
    output = result.stdout
    offset = 0
    issues = []
    for expected in ids:
        end = output.index(b"\n", offset)
        oid, kind, size = output[offset:end].decode("ascii").split()
        if oid != expected or kind != "blob":
            raise RuntimeError("Unexpected Git object response")
        offset = end + 1
        data = output[offset:offset + int(size)]
        offset += int(size) + 1
        for number in sorted(credential_lines(data)):
            for name in sorted(objects[oid]):
                issues.append(f"history:{name}:{number} ({oid[:12]}): credential pattern")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="同时检查所有本地可达提交")
    args = parser.parse_args()
    issues = []
    checked = 0
    for name in current_files():
        path = ROOT / name
        if path.is_symlink():
            issues.append(f"{name}: symlink requires manual review")
            continue
        if not path.exists():
            continue  # Tracked deletion in the current working tree.
        if not path.is_file():
            issues.append(f"{name}: unsupported non-file entry")
            continue
        checked += 1
        if private_path(name):
            issues.append(f"{name}: private data path")
        data = path.read_bytes()
        for number in sorted(credential_lines(data)):
            issues.append(f"{name}:{number}: credential pattern")
        if path.suffix.lower() == ".md":
            issues.extend(document_links(name, data))
    commits = blobs = 0
    if args.history:
        objects, path_issues, commits = history_blobs()
        blobs = len(objects)
        issues.extend(path_issues)
        issues.extend(scan_history(objects))
    print(f"Checked {checked} current files; {commits} commits / {blobs} historical blobs.")
    if issues:
        print("Repository check failed (matched content is never printed):")
        for issue in sorted(set(issues)):
            print("- " + issue)
        return 1
    print("OK: no blocked paths, credential patterns or broken local Markdown links found.")
    print("Review staged content manually; this check is not a complete privacy audit.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        print("Repository check could not finish: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
