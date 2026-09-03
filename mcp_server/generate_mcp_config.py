#!/usr/bin/env python3
"""
Writes mcp_client_config.json for THIS machine, from
mcp_client_config.example.json's shape -- fills in the absolute paths
to this machine's mcp_server/venv and server.py automatically, so
nobody has to hand-edit machine-specific paths (or accidentally commit
someone else's) to run the AI buyer demo.

Run:
    python generate_mcp_config.py

Then point your MCP client's config at the mcp_client_config.json
this writes.
"""

import json
import platform
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / "venv"
PYTHON = VENV / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
SERVER = HERE / "server.py"

config = {
    "mcpServers": {
        "demo-merchant": {
            "command": str(PYTHON),
            "args": [str(SERVER)],
            "env": {"BACKEND_URL": "http://127.0.0.1:8123"},
        }
    }
}

out_path = HERE / "mcp_client_config.json"
out_path.write_text(json.dumps(config, indent=2) + "\n")

print(f"Wrote {out_path}")
if not PYTHON.exists():
    print(f"NOTE: {PYTHON} does not exist yet -- create the venv and install "
          f"requirements first (see README.md's 'AI buyer demo (MCP)' section), "
          f"then re-run this script.")
print("Point your MCP client's config at this file, then restart the client.")
