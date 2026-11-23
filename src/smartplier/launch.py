"""Start the local dashboard, wait for readiness and open the default browser."""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADDRESS = "http://127.0.0.1:8010"


def ready():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(ADDRESS + "/api/overview", timeout=1) as reply:
            return isinstance(json.load(reply).get("csrf"), str)
    except (OSError, ValueError, AttributeError):
        return False


def main():
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("Production assets are missing. Run npm --prefix frontend ci and npm --prefix frontend run build.")
        return 1
    if not ready():
        process = subprocess.Popen([sys.executable, "-B", str(ROOT / "jobhunt.py"), "web", "--port", "8010"],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not ready():
            if process.poll() is not None:
                break
            time.sleep(.25)
        if not ready():
            print("The dashboard could not start. Run python jobhunt.py web to see the startup error.")
            return 1
    print(f"Smartplier is ready at {ADDRESS}")
    if not os.getenv("SMARTPLIER_NO_BROWSER"):
        webbrowser.open(ADDRESS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
