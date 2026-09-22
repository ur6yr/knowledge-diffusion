"""Read-only, bounded inventory for the current process and configured paths."""

import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess


def inspect_environment(paths=(), *, gpu=False):
    versions = {}
    for package in ['autogen-agentchat', 'autogen-core', 'autogen-ext', 'neo4j', 'psycopg', 'redis', 'openai', 'openpyxl']:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    result = {'kind': 'preflight-v1', 'hostname': socket.gethostname(), 'platform': platform.platform(),
              'python': platform.python_version(), 'packages': versions,
              'job_id': os.environ.get('SLURM_JOB_ID'), 'partition': os.environ.get('SLURM_JOB_PARTITION'),
              'allocated_cpus': os.environ.get('SLURM_CPUS_PER_TASK'), 'allocated_memory_mb': os.environ.get('SLURM_MEM_PER_NODE'),
              'visible_gpu_selector': os.environ.get('CUDA_VISIBLE_DEVICES'),
              'loaded_modules': os.environ.get('LOADEDMODULES', '').split(':'),
              'credentials': {k: bool(os.environ.get(k)) for k in ['OPENAI_API_KEY', 'VLLM_API_KEY', 'KDIFF_POSTGRES_DSN', 'KDIFF_REDIS_URL']},
              'paths': [], 'limitations': ['Filesystem free bytes are not a user quota measurement.',
                  'This inventory does not establish cluster network, model capability or multi-node compatibility.']}
    for value in paths:
        path = Path(value).resolve()
        entry = {'path': str(path), 'exists': path.exists(), 'symlink': Path(value).is_symlink()}
        parent = path if path.is_dir() else path.parent
        if parent.exists():
            disk = shutil.disk_usage(parent)
            entry.update(filesystem_free_bytes=disk.free, filesystem_total_bytes=disk.total)
        result['paths'].append(entry)
    if gpu:
        binary = shutil.which('nvidia-smi')
        if binary:
            inspected = subprocess.run([binary, '--query-gpu=name,memory.total,memory.free,driver_version', '--format=csv,noheader'],
                                       capture_output=True, text=True, timeout=15)
            result['gpu'] = {'status': 'available' if inspected.returncode == 0 else 'failed',
                             'inventory': inspected.stdout.strip().splitlines() if inspected.returncode == 0 else []}
        else:
            result['gpu'] = {'status': 'nvidia-smi-unavailable'}
    return result
