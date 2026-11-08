"""Record software and native binary provenance without altering runtime settings."""
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import sysconfig

import turbovec
import raggio.app as api
import raggio.store as store

pkg=Path(turbovec.__file__).parent
result={'python':sys.version,'machine':platform.machine(),'sqlite':sqlite3.sqlite_version,
    'gil_enabled':getattr(sys,'_is_gil_enabled',lambda:True)(),
    'gil_disabled_build':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
    'extension_suffix':sysconfig.get_config_var('EXT_SUFFIX'),
    'openblas_num_threads':os.environ.get('OPENBLAS_NUM_THREADS'),
    'cached_statements':store.SQLITE_CACHED_STATEMENTS,
    'versions':{name:metadata.version(name) for name in ('numpy','turbovec','fastapi','pydantic','raggio-native')},
    'turbovec_extensions':{str(p.relative_to(pkg)):hashlib.sha256(p.read_bytes()).hexdigest() for p in pkg.rglob('*.so')},
    'store_lf_sha256':hashlib.sha256(Path(store.__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
    'api_lf_sha256':hashlib.sha256(Path(api.__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest()}
print(json.dumps(result,indent=2))
