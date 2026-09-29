import json
import urllib.request

from yt_clipper.core.log import describe_failure, get_logger

logger = get_logger(__name__)

API_URL = "https://api.github.com/repos/{owner}/{repo}/releases/latest"

def check_for_update(owner, repo, current_version, timeout=4):
    """Returns (is_newer, latest_version, html_url).

    Failure is recoverable and never fatal (§13): an unreachable API, a blocked
    network or an unexpected response shape all degrade to
    (False, None, None) - "no update known" - and the reason is recorded at
    DEBUG, because this is an optional background check the user never asked for
    and it must not interfere with clipping.
    """
    url = API_URL.format(owner=owner, repo=repo)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", url)
        return (bool(latest) and _is_newer(latest, current_version)), latest, html_url
    except Exception as exc:
        logger.debug("Update check skipped (%s): %s", url, describe_failure(exc))
        return False, None, None

def _is_newer(latest, current):
    def parse(v):
        return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split("."))
    return parse(latest) > parse(current)