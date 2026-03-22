"""
Entry point — installs deps, starts the server, opens the browser.
Run: python3 main.py
"""

from __future__ import annotations

import os
import sys
import time
import subprocess
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("PORT", 8765))
BASE = Path(__file__).parent


def check_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("\n[ERROR] ANTHROPIC_API_KEY is not set.")
        print("    Export it before running:")
        print('    export ANTHROPIC_API_KEY="sk-ant-..."')
        sys.exit(1)
    print(f"[OK] API key found ({key[:12]}...)")


def install_deps():
    req = BASE / "requirements.txt"
    print("Checking dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req), "-q"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[WARN] Dependency install warning:", result.stderr[:200])
    else:
        print("[OK] Dependencies ready")


def check_claude_cli():
    import shutil
    if shutil.which("claude"):
        print("[OK] Claude Code CLI detected — will use for code generation")
    else:
        print("[WARN] Claude Code CLI not found — using Claude API fallback")
        print("    To install: npm install -g @anthropic-ai/claude-code")


def start_server():
    is_hosted = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
    print(f"\n🚀  Starting Builder Bot on http://localhost:{PORT}")
    print("    Press Ctrl+C to stop\n")

    # Only open browser when running locally
    if not is_hosted:
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{PORT}")
        import threading
        threading.Thread(target=open_browser, daemon=True).start()

    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="warning",
        app_dir=str(BASE),
    )


if __name__ == "__main__":
    print("=" * 50)
    print("  Builder Bot — Autonomous Project Engineer")
    print("=" * 50)
    is_hosted = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
    if not is_hosted:
        # Locally: check key + install deps
        check_api_key()
        install_deps()
        check_claude_cli()
    else:
        print("Running in hosted mode")
    start_server()
