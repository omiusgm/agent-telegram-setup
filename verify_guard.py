"""Run local in-process fixtures only; no live backend or child process."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from verify_read_draft import main

if __name__ == "__main__":
    main()
