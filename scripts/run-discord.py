"""Absolute-path entry point used by the Windows supervisor."""
from pathlib import Path
import os

os.chdir(Path(__file__).resolve().parents[1])
from padelbot.cli import cli

if __name__ == "__main__":
    cli(["discord"])
