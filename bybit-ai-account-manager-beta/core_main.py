from __future__ import annotations
from config import load_local_env
load_local_env()
from core_server import run_server

if __name__ == "__main__":
    run_server()
