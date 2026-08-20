from __future__ import annotations

import os
import sys

# Desktop entrypoint kept intentionally small.
from config import load_local_env

load_local_env()


def main() -> None:
    from gui_v2 import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
