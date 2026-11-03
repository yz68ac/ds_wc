"""Private image artifacts and integrity checks, independent of model services."""
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid


class ImageArtifactError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


MAX_BYTES = 8 * 1024 * 1024
FILE_NAME = re.compile(r'[a-f0-9]{32}\.(?:png|jpg)\Z')


def artifact_directory(queue_file):
    root = Path(queue_file).parent/'generated-images'
    if root.is_symlink():
        raise ImageArtifactError('image_artifact_unavailable')
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and
            (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ImageArtifactError('image_artifact_unavailable')
    return root


def read_artifact(queue_file, image):
    name = image.get('file')
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        raise ImageArtifactError('image_artifact_unavailable')
    try:
        path = artifact_directory(queue_file)/name
        if path.is_symlink():raise ImageArtifactError('image_artifact_unavailable')
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_BYTES
                    or (os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ImageArtifactError('image_artifact_unavailable')
            data = stream.read(MAX_BYTES+1)
        if len(data) != image['bytes'] or hashlib.sha256(data).hexdigest() != image['sha256']:
            raise ImageArtifactError('image_artifact_changed')
    except (OSError, KeyError):
        raise ImageArtifactError('image_artifact_unavailable') from None
    return path, data


def artifact_path(queue_file, image):
    return read_artifact(queue_file, image)[0]


def write_artifact(queue_file, data, metadata):
    name = uuid.uuid4().hex+('.png' if metadata['mime'] == 'image/png' else '.jpg')
    path = artifact_directory(queue_file)/name
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:path.unlink(missing_ok=True)
        raise
    return dict(file=name, **metadata)
