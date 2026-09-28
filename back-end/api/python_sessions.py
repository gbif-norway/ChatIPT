"""Per-agent persistence of Python tool variables between calls."""

import logging
import os
import pickle
import tempfile
import types
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path

from django.conf import settings


logger = logging.getLogger(__name__)

MAX_VARIABLE_BYTES = 200 * 1024 * 1024
MAX_SESSION_BYTES = 500 * 1024 * 1024
MAX_CACHED_UPLOADS = 4
_upload_cache = OrderedDict()


def _root():
    return Path(getattr(settings, 'PYTHON_SESSION_ROOT', Path(settings.BASE_DIR) / 'python_sessions'))


def _path(agent_id):
    return _root() / f'agent-{int(agent_id)}.pkl'


def load(agent_id):
    path = _path(agent_id)
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as handle:
            return pickle.load(handle)
    except Exception:
        logger.exception('Discarding unreadable Python session for agent %s', agent_id)
        path.unlink(missing_ok=True)
        return {}


def save(agent_id, namespace, reserved):
    """Keep picklable user variables; return the names that could not be kept."""
    kept, skipped, total = {}, [], 0
    for name, value in namespace.items():
        if name.startswith('_') or name in reserved or isinstance(value, types.ModuleType):
            continue
        if isinstance(value, (types.FunctionType, type)):
            skipped.append(name)
            continue
        try:
            data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            skipped.append(name)
            continue
        if len(data) > MAX_VARIABLE_BYTES or total + len(data) > MAX_SESSION_BYTES:
            skipped.append(name)
            continue
        kept[name] = value
        total += len(data)
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix='.session-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            pickle.dump(kept, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, _path(agent_id))
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return skipped


def clear(agent_id):
    _path(agent_id).unlink(missing_ok=True)


def _extracted_copy(user_file):
    """Parse each upload once per worker process; hand out copies so edits never leak."""
    key = (user_file.id, user_file.file.name, user_file.uploaded_at)
    if key in _upload_cache:
        _upload_cache.move_to_end(key)
    else:
        _upload_cache[key] = user_file.extract_data()[1]
        while len(_upload_cache) > MAX_CACHED_UPLOADS:
            _upload_cache.popitem(last=False)
    return {sheet: None if frame is None else frame.copy() for sheet, frame in _upload_cache[key].items()}


def tabular_source_files(dataset):
    """Tabular uploads keyed as `sources` exposes them; a repeated filename gets its file id."""
    files = {}
    for user_file in dataset.user_files.order_by('id'):
        if user_file.file_type != user_file.FileType.TABULAR:
            continue
        name = user_file.filename
        if name in files:
            name = f'{name} (file {user_file.id})'
        files[name] = user_file
    return files


class SourceFiles(Mapping):
    """Original uploads by filename, each a {sheet_name: DataFrame} dict, loaded on first use."""

    def __init__(self, dataset):
        self._files = tabular_source_files(dataset)
        self._loaded = {}
        self._comments = {}

    def __getitem__(self, name):
        if name not in self._loaded:
            self._loaded[name] = _extracted_copy(self._files[name])
        return self._loaded[name]

    def __iter__(self):
        return iter(self._files)

    def __len__(self):
        return len(self._files)

    def cell_comments(self, name):
        """Every cell comment in an uploaded workbook, as {sheet_name: [comment, ...]}."""
        if name not in self._comments:
            user_file = self._files[name]
            user_file.extract_data()
            self._comments[name] = getattr(user_file, '_excel_comments', None) or {}
        return {sheet: list(comments) for sheet, comments in self._comments[name].items()}

    def __repr__(self):
        return f'SourceFiles({list(self._files)})'
