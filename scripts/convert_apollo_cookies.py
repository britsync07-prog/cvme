import json
import sys
from pathlib import Path


def main(source: str, account_email: str, destination: str) -> None:
    raw = json.loads(Path(source).read_text(encoding="utf-8"))
    cookies = raw.get("cookies", []) if isinstance(raw, dict) else raw
    converted = []
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        if "apollo.io" not in domain or not cookie.get("name"):
            continue
        converted.append({
            "name": cookie["name"], "value": cookie.get("value", ""), "domain": domain,
            "path": cookie.get("path", "/"),
            "httpOnly": bool(cookie.get("httpOnly", False)), "secure": bool(cookie.get("secure", False)),
            "session": "expirationDate" not in cookie,
        })
    Path(destination).write_text(json.dumps({account_email: converted}), encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:])
