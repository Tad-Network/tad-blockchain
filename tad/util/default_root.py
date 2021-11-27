import os
from pathlib import Path

DEFAULT_ROOT_PATH = Path(os.path.expanduser(os.getenv("TAD_ROOT", "~/.tad/mainnet"))).resolve()

DEFAULT_KEYS_ROOT_PATH = Path(os.path.expanduser(os.getenv("TAD_KEYS_ROOT", "~/.tad_keys"))).resolve()
