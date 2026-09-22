"""Restricted atomic readiness files for owned services."""

import json
import os
from uuid import uuid4


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + str(uuid4()) + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
