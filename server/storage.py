"""Atomic snapshots for the single-process demo server."""
import json
import os
import tempfile
import hashlib


def atomic_write_json(path, data):
    """Keep the previous complete snapshot if serialization or replacement fails."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.fleet-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def store_release(stream, path, max_bytes=256 * 1024 * 1024):
    """Publish a complete package without replacing an existing release."""
    temporary = None
    checksum = hashlib.sha256()
    size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), prefix='.upload-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise OverflowError('Firmware package exceeds the 256 MB upload limit')
                checksum.update(chunk)
                handle.write(chunk)
            if size == 0:
                raise ValueError('Firmware package is empty')
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic create-if-absent, including concurrent uploads of the same version.
        os.link(temporary, path)
        return {'size': size, 'sha256': checksum.hexdigest()}
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
