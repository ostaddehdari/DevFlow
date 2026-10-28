"""Internal read-only worker. No shell or arbitrary command dispatch."""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from .contracts import Contracts, DevFlowError
from .operation_plans import PlanPolicy


MAX_READ = 1048576


def read_digest(root, relative):
    root = Path(root).resolve()
    policy = PlanPolicy(root, Contracts(root / 'contracts'))
    policy.target(relative)
    parts = relative.split('/')
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptor)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_READ:
            raise DevFlowError('Read target must be a regular file of at most 1 MiB')
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(fd, min(65536, MAX_READ + 1 - size))
            if not block:
                break
            size += len(block)
            if size > MAX_READ:
                raise DevFlowError('Read target exceeds 1 MiB')
            digest.update(block)
        return {'sha256': digest.hexdigest(), 'bytes': size}
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--path', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(read_digest(args.root, args.path)))
        return 0
    except (DevFlowError, OSError):
        print('Read operation denied or unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
