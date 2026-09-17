import json
import urllib.request

API_URL = "https://api.github.com/repos/{owner}/{repo}/releases/latest"

def check_for_update(owner, repo, current_version, timeout=4):
    """Returns (is_newer, latest_version, html_url). Fails silently -> (False, None, None)."""
    url = API_URL.format(owner=owner, repo=repo)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", url)
        return (bool(latest) and _is_newer(latest, current_version)), latest, html_url
    except Exception:
        return False, None, None

def _is_newer(latest, current):
    def parse(v):
        return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split("."))
    return parse(latest) > parse(current)