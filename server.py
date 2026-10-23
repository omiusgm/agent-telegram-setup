"""Absolute-path entrypoint for MCP clients with any working directory."""

import sys
from telegram_setup.relay import main

if __name__ == "__main__":
    sys.exit(main())

