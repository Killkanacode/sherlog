"""Container HEALTHCHECK probe (the slim image has no curl)."""

import os
import sys
import urllib.request

try:
    urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/health", timeout=3)
except Exception:
    sys.exit(1)
