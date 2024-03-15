from __future__ import annotations

import json
import shutil
from itertools import count
from pathlib import Path
from datetime import datetime
from typing import Optional

from pharmpy.deps import pandas as pd
from pharmpy.internals.fs.path import path_absolute
from pharmpy.internals.fs.symlink import create_directory_symlink
from pharmpy.internals.fs.lock import path_lock
from pharmpy.model import Model
from pharmpy.workflows.results import ModelfitResults
from pharmpy.workflows.hashing import ModelHash

from ..model_database import LocalModelDirectoryDatabase
from ..results import read_results
from .baseclass import Context


class LocalDirectoryContext(Context):
    """Context in a local directory

    Parameters
    ----------
    name : str
        Name of the context
    path : str or Path
        Path to directory. Will be created if it does not exist.
    exist_ok : bool
        Whether to allow using an existing context.
    """

    def __init__(self, name: Optional[str]=None, parent: Optional[LocalDirectoryContext]=None, path: Optional[Union[str, Path]]=None, exists_ok: bool = True):
        # Give name, parent to create a subcontext
        # Give path to create a top level context or open an already available context
        if name is not None and parent is not None and path is None:
            path = parent.path / 'subcontexts' / name
        elif path is not None and name is None and parent is None:
            pass
        else:
            raise ValueError("Either supply name and parent or path")

        self.path = path_absolute(path)
        if self.path.is_dir():
            if not exists_ok:
                raise ValueError("Context already exists")
        else:
            self.path.mkdir(parents=True)

        if not (self.path / 'subcontexts').is_dir():
            (self.path / 'subcontexts').mkdir()

        self._init_top_path()

        self._model_database = LocalModelDirectoryDatabase(self._top_path / '.modeldb')

        self._init_annotations()
        self._init_model_name_map()
        self._init_log(parent)

        super().__init__(name)

    def _init_top_path(self):
        path = self.path
        while True:
            parent = path.parent
            if path == parent:
                raise FileNotFoundError("Cannot find top level of context.")
            if not (parent.name == "subcontexts"):
                self._top_path = path
                break
            path = parent.parent

    def _init_annotations(self):
        path = self._annotations_path
        if not path.is_file():
            path.touch()

    def _init_model_name_map(self):
        self._models_path.mkdir(exist_ok=True)


    def _init_log(self, parent):
        if parent is None:
            log_path = self._log_path
            if not log_path.is_file():
                with open(log_path, 'w') as fh:
                    fh.write("path,time,severity,message\n")

    def _read_lock(self, path: Path):
        # NOTE: Obtain shared (blocking) lock on one file
        path = path.with_suffix('.lock')
        path.touch(exist_ok=True)
        return path_lock(str(path), shared=True)

    def _write_lock(self, path : Path):
        # NOTE: Obtain exclusive (blocking) lock on one file
        path = path.with_suffix('.lock')
        path.touch(exist_ok=True)
        return path_lock(str(path), shared=False)

    def store_results(self, res: Results):
        res.to_json(path=self.path / 'results.json')
        res.to_csv(path=self.path / 'results.csv')

    def retrieve_results(self) -> Results:
        res = read_results(self.path / 'results.json')
        return res

    @property
    def _log_path(self) -> Path:
        return self._top_path / 'log'

    @property
    def _metadata_path(self) -> Path:
        return self.path / 'metadata.json'

    @property
    def _models_path(self) -> Path:
        return self.path / 'models'

    @property
    def _annotations_path(self) -> Path:
        return self.path / '.annotations'

    @property
    def context_path(self) -> str:
        relpath = self.path.relative_to(self._top_path.parent)
        posixpath = str(relpath.as_posix())
        a = posixpath.split('/')[0::2]    # Remove subcontexts/
        ctxpath = '/'.join(a)
        return ctxpath

    def store_metadata(self, metadata: dict):
        with open(self._metadata_path, 'w') as f:
            json.dump(metadata, f, indent=4, cls=MetadataJSONEncoder)

    def retrieve_metadata(self) -> dict:
        with open(self._metadata_path, 'r') as f:
            return json.load(f, cls=MetadataJSONDecoder)

    def store_key(self, name: str, key: ModelHash):
        # FIXME: check if exists
        from_path = self._models_path / name
        if not from_path.exists():
            create_directory_symlink(self._models_path / name, self.model_database.path / str(key))

    def retrieve_key(self, name: str) -> ModelHash:
        symlink_path = self._models_path / name
        digest = symlink_path.resolve().name
        db = self.model_database
        # FIXME: Currently it is not possible to use the digest here in the modeldb
        with db.snapshot(ModelHash(digest)) as txn:
            key = txn.key
        return key

    def retrieve_name(self, key: ModelHash) -> str:
        path = self._models_path
        mydigest = str(key)
        for link_path in path.iterdir():
            resolved = link_path.resolved()
            digest = resolved.name
            if digest == mydigest:
                return link_path.name
        raise KeyError("Model with key {mydigest} could not be found.")

    def store_annotation(self, name: str, annotation: str):
        path = self._annotations_path
        with self._write_lock(path):
            with open(path, 'r') as fh:
                lines = []
                found = False
                for line in fh.readlines():
                    a = line.split(" ", 1)
                    if a[0] == name:
                        lines.append(f'{name} {annotation}\n')
                        found = True
                    else:
                        lines.append(line)
                if not found:
                    lines.append(f'{name} {annotation}\n')
            with open(path, 'w') as fh:
                fh.writelines(lines)

    def retrieve_annotation(self, name: str) -> str:
        path = self._annotations_path
        with self._read_lock(path):
            with open(path, 'r') as fh:
                for line in fh.readlines():
                    a = line.split(" ", 1)
                    if a[0] == name:
                        return a[1][:-1]
        raise KeyError(f"No annotation for {name} available")

    def log_message(self, severity, msg: str):
        log_path = self._log_path
        with self._write_lock(log_path):
            with open(log_path, 'a') as fh:
                fh.write(f'{self.context_path},{datetime.now()},{severity},{msg}\n')

    def retrieve_log(self, level: Literal['all', 'current', 'lower']='all') -> pd.DataFrame:
        log_path = self._log_path
        with self._read_lock(log_path):
            # NOTE: Custom parsing to allow message column to contain any character
            with open(log_path, 'r') as fh:
                lines = []
                for line in fh.readlines():
                    a = line[0:-1].split(',')
                    if len(a) > 4:
                        a[3] = ','.join(a[3:])
                        del a[4:]
                    lines.append(a)
                df = pd.DataFrame(lines[1:], columns=lines[0])
        count = df['path'].str.count('/')
        curlevel = self.context_path.count('/')
        if level == 'lower':
            df = df.loc[count >= curlevel]
        elif level == 'current':
            df = df.loc[count == curlevel]
        df = df.reset_index(drop=True)
        return df

    def get_parent_context(self) -> LocalDirectoryContext:
        if self.path == self._top_path:
            raise ValueError("Already at the top level context")
        parent = LocalDirectoryContext(path=self.path.parent.parent, exists_ok=True)
        return parent

    def get_subcontext(self, name: str) -> LocalDirectoryContext:
        path = self.path / 'subcontexts' / name
        if path.is_dir():
            return LocalDirectoryContext(path=path, exists_ok=True)
        else:
            raise ValueError(f"No subcontext with the name {name}")

    def create_subcontext(self, name: str) -> LocalDirectoryContext:
        ctx = LocalDirectoryContext(name=name, parent=self, exists_ok=False)
        return ctx


class MetadataJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Model):
            # NOTE: This is only used by modelfit at the moment since we encode
            # models for other tools upstream.
            return obj.name
        if isinstance(obj, ModelfitResults):
            return obj.to_json()
        return super().default(obj)


class MetadataJSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        json.JSONDecoder.__init__(self, object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, obj):
        return obj
