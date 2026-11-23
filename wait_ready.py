"""Hold systemd startup until the authenticated private MCP listener is ready."""
import time
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    try:
        with urlopen('http://127.0.0.1:18765/mcp', timeout=1):
            pass
    except HTTPError as error:
        if error.code == 401:
            break
    except (URLError, TimeoutError):
        pass
    time.sleep(0.25)
else:
    raise SystemExit('Private MCP listener did not become ready')
