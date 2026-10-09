#!/usr/bin/env python3
"""Detect a proxy route matcher that SHADOWS a real frontend file.

The bug this exists to prevent
------------------------------
The vhost proxied `/tts*` to the API. `/tts` is a backend route -- but `/tts.js` is a
real frontend file, and a PREFIX matcher cannot tell them apart. The browser asked for
JavaScript, got the API's 404 JSON, refused to execute it, so `stopSpeaking` was never
defined. `liveFreeTurn()` calls `stopSpeaking()` as its first statement, so every send
threw ReferenceError and the chat died -- while `/turn` itself stayed perfectly healthy.
One character (`*`, or a missing `=`) produced two unrelated-looking symptoms.

What it checks
--------------
Every static file the browser can request is tested against every PREFIX matcher in the
web-server config. An exact matcher (`path /tts`, `location = /tts`) is safe by
construction and is not reported. Anything that would be swallowed is a hard failure.

Usage
-----
    python3 check_route_shadowing.py --config <Caddyfile|provision.sh> --frontend <dir>

Exit 0 = no shadowing. Exit 1 = at least one static file is unreachable. Exit 2 = usage.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Matcher extraction
#
# Only a matcher whose block actually PROXIES can shadow a static file. nginx's
# `location / { try_files ...; }` is the static catch-all and is the point of the
# whole config, not a bug -- so blocks are brace-matched and filtered on the
# presence of a proxy directive before they are considered.
# ---------------------------------------------------------------------------

# `handle /tts* {` or `handle @api {` -- the matcher is the first token.
CADDY_HANDLE = re.compile(r"^\s*handle\s+(\S+)\s*\{", re.M)
# `location /tts {`, `location = /tts {`, `location ~ ^/x(/|$) {`
NGINX_LOCATION = re.compile(r"^\s*location\s+(.+?)\s*\{", re.M)
# `@api path /turn /confirm ...` -- exact paths in a named Caddy matcher.
CADDY_PATH_MATCHER = re.compile(r"^\s*(?:@[\w-]+\s+)?path\s+(.+)$", re.M)

CADDY_PROXY_DIRECTIVE = "reverse_proxy"
NGINX_PROXY_DIRECTIVE = "proxy_pass"


def _block_body(text: str, open_brace_index: int) -> str:
    """Return the text between the brace at open_brace_index and its match."""
    depth = 0
    for i in range(open_brace_index, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index + 1:i]
    return text[open_brace_index + 1:]


def proxying_matchers(text: str) -> list[tuple[str, str]]:
    """[(matcher, 'caddy'|'nginx')] for every block that proxies."""
    out: list[tuple[str, str]] = []
    for m in CADDY_HANDLE.finditer(text):
        body = _block_body(text, m.end() - 1)
        if CADDY_PROXY_DIRECTIVE in body:
            out.append((m.group(1).strip(), "caddy"))
    for m in NGINX_LOCATION.finditer(text):
        body = _block_body(text, m.end() - 1)
        if NGINX_PROXY_DIRECTIVE in body:
            out.append((m.group(1).strip(), "nginx"))
    return out


def prefix_matchers(text: str) -> list[str]:
    """Prefixes a proxying matcher would swallow. Exact/regex matchers are excluded."""
    prefixes: list[str] = []
    for matcher, flavour in proxying_matchers(text):
        if flavour == "caddy":
            # `@name` is a named matcher -- its paths are checked separately.
            if matcher.startswith("@"):
                continue
            # Only a trailing `*` makes it a prefix; a bare path is an exact match.
            if matcher.endswith("*"):
                prefixes.append(matcher[:-1])
        else:
            # `= /tts` is exact, `~ ...` is regex -- neither is a prefix.
            if matcher.startswith("=") or matcher.startswith("~"):
                continue
            if matcher.startswith("/"):
                prefixes.append(matcher)
    return sorted(set(prefixes))


def exact_paths(text: str) -> list[str]:
    """Exact paths from Caddy named `path` matchers (wildcards kept, they are noted)."""
    out: list[str] = []
    for m in CADDY_PATH_MATCHER.finditer(text):
        for token in m.group(1).split():
            token = token.strip()
            if token.startswith("/"):
                out.append(token)
    return out


def regex_matchers(text: str) -> list[str]:
    """Regex matchers, whose missing boundaries can shadow too."""
    out: list[str] = []
    for m in NGINX_LOCATION.finditer(text):
        matcher = m.group(1).strip()
        if matcher.startswith("~"):
            out.append(matcher.lstrip("~* ").strip())
    return out


# ---------------------------------------------------------------------------
# Static file inventory
# ---------------------------------------------------------------------------


def static_files(frontend: Path) -> list[str]:
    """URL paths the browser can request: top-level files plus one level of assets."""
    found: list[str] = []
    if not frontend.is_dir():
        return found
    for p in sorted(frontend.iterdir()):
        if p.is_file():
            found.append("/" + p.name)
        elif p.is_dir():
            for sub in sorted(p.iterdir()):
                if sub.is_file():
                    found.append(f"/{p.name}/{sub.name}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Caddyfile, nginx conf, or provision.sh")
    ap.add_argument("--frontend", required=True, help="frontend directory")
    args = ap.parse_args()

    cfg = Path(args.config)
    front = Path(args.frontend)
    if not cfg.is_file():
        print(f"config not found: {cfg}", file=sys.stderr)
        return 2

    text = cfg.read_text()

    prefixes = prefix_matchers(text)
    exacts = set(exact_paths(text))
    regexes = regex_matchers(text)

    print(f"config   : {cfg}")
    print(f"frontend : {front}")
    print(f"prefix matchers : {prefixes or '(none)'}")
    print(f"exact matchers  : {sorted(exacts) or '(none)'}")
    print(f"regex matchers  : {regexes or '(none)'}")
    print()

    files = static_files(front)
    print(f"{len(files)} static file(s) to check\n")

    failures: list[tuple[str, str]] = []

    for url in files:
        # A prefix matcher swallows anything that merely STARTS WITH it.
        for pre in prefixes:
            if url.startswith(pre):
                # `/tts` vs `/tts.js`: the file is longer, so the prefix wins and the
                # static file is unreachable. An exact matcher would not do this.
                if url != pre:
                    failures.append((url, f"prefix '{pre}*' swallows it"))
                    break
        else:
            # A regex matcher with no boundary also swallows, e.g.
            # ^/(turn|scenarios|...) without (/|$) would capture /scenarios.js.
            for rx in regexes:
                try:
                    if re.search(rx, url):
                        failures.append((url, f"regex '{rx}' matches it"))
                        break
                except re.error:
                    continue

    if failures:
        print("\033[31mSHADOWED -- these static files can never be served:\033[0m")
        for url, why in failures:
            print(f"  FAIL {url:34} {why}")
        print(
            "\nFix: make the route an EXACT match -- `path /tts` in Caddy, "
            "`location = /tts` in nginx."
        )
        return 1

    print("\033[32mPASS -- every static file is reachable; no proxy matcher shadows one.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
