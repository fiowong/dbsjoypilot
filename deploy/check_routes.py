#!/usr/bin/env python3
"""Cross-check the Nginx proxy rules against the routes the backend actually defines.

    python3 deploy/check_routes.py

WHY THIS EXISTS
    The Nginx vhost in provision.sh enumerates the API routes it proxies. Every route NOT in
    that list falls through to `location /`, which serves index.html with HTTP 200. The caller
    then gets a page of HTML where it expected JSON, and the browser reports a confusing parse
    error rather than a 404 -- a genuinely nasty failure to debug.

    This was not hypothetical: /session/{session_id} was defined in main.py but absent from the
    proxy list. This script makes that class of drift impossible to miss.

Exit code 0 = every backend route is reachable through Nginx.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "backend" / "app" / "main.py"
PROVISION_SH = ROOT / "deploy" / "provision.sh"

# Routes that get a dedicated `location` block instead of the shared API regex.
#
# These MUST be EXACT matches (`location = /tts`). A bare prefix `location /tts` also
# captures `/tts.js` -- a real frontend file -- so the browser gets the API's 404 JSON
# where it expects JavaScript, `stopSpeaking` is never defined, and every chat send dies
# with ReferenceError while /turn stays perfectly healthy on the wire. Measured, not
# guessed: that exact bug took the live site's chat down. See check_route_shadowing.py.
DEDICATED = {"/tts": "/tts", "/ws/stt": "/ws/stt"}

# ---------------------------------------------------------------------------
# Caddy. The SAME drift happens here, and nothing checked it -- so it did.
#
# /tts/provider was added to the backend while the Caddy `@api path` list still ended at
# /session/*. On the live site the UI's provider switch therefore POSTed into the static
# handler and got index.html back with a 200. The frontend then failed to parse it as JSON
# and reverted the toggle, so the new "TRTC Mode" control would have looked like it did
# nothing at all -- with no error anywhere. Nginx was guarded by this file; Caddy was not.
#
# A Caddy named matcher that must stay EXACT: `@tts path /tts` is safe, whereas
# `@tts path /tts*` would also capture /tts.js -- the bug that took the chat down once.
# ---------------------------------------------------------------------------
CADDY_API_LINE = re.compile(r"^\s*@api\s+path\s+(.+?)\s*$", re.M)
CADDY_NAMED_LINE = re.compile(r"^\s*@(\w+)\s+path\s+(.+?)\s*$", re.M)
CADDY_DEDICATED = {"/tts": "tts", "/ws/stt": "stt"}


def caddy_pattern_to_regex(pattern: str) -> str:
    """Compile one Caddy `path` pattern. Literal, except `*` matches any run of characters.

    `*` deliberately spans `/` too, so `/tts/*` covers `/tts/provider`. That is safe only
    because the literal `/` before the wildcard is what stops it also matching `/tts.js`.
    """
    return re.escape(pattern).replace(r"\*", ".*") + r"\Z"


def caddy_check(routes: list[str], source: str) -> tuple[int, int]:
    """Every backend route must be reachable through the Caddy vhost as well."""
    api_match = CADDY_API_LINE.search(source)
    if api_match is None:
        print("  FAIL  could not find the `@api path` matcher in provision.sh")
        return 1, len(routes)

    api_patterns = api_match.group(1).split()
    compiled = [re.compile(caddy_pattern_to_regex(p)) for p in api_patterns]
    named = {m.group(1): m.group(2).strip() for m in CADDY_NAMED_LINE.finditer(source)}

    print(f"  Caddy @api path : {' '.join(api_patterns)}")
    print("")

    failures = 0
    for route in routes:
        concrete = re.sub(r"\{[^}]+\}", "TEST", route)
        if route in CADDY_DEDICATED:
            want = CADDY_DEDICATED[route]
            got = named.get(want)
            if got == route:
                ok, reason = True, f"dedicated `@{want} path {route}` (exact match)"
            elif got is None:
                ok, reason = False, f"MISSING dedicated `@{want} path {route}` matcher"
            else:
                ok, reason = False, f"`@{want} path {got}` is a PREFIX -- it also captures {route}.js"
        else:
            ok = any(c.match(concrete) for c in compiled)
            reason = "shared @api matcher"

        if not ok:
            failures += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {route:<28} via {reason}")
        if not ok:
            print(f"        -> {concrete} would fall through to the static handler "
                  f"and return index.html")

    # The dedicated matchers must not also be swallowed by @api, or which one wins depends
    # on the order of the handle blocks.
    for route in CADDY_DEDICATED:
        if any(c.match(route) for c in compiled):
            failures += 1
            print(f"  FAIL  {route} is matched by @api as well as its own named matcher")

    return failures, len(routes) + len(CADDY_DEDICATED)


def dedicated_form(path: str, source: str) -> str | None:
    """'exact' if `location = <path>` is present, 'prefix' for a bare `location <path>`."""
    if re.search(rf"location\s+=\s+{re.escape(path)}\s*\{{", source):
        return "exact"
    if re.search(rf"location\s+{re.escape(path)}\s*\{{", source):
        return "prefix"
    return None


def backend_routes() -> list[str]:
    source = MAIN_PY.read_text(encoding="utf-8")
    # NOTE: do NOT anchor on a closing paren. Several decorators carry extra arguments,
    # e.g. @app.post("/turn", response_model=TurnResponse), and an anchored pattern
    # silently skips them -- which is exactly the drift this script is meant to catch.
    found = re.findall(r'@app\.(?:get|post|put|delete|websocket)\("([^"]+)"', source)
    # Preserve definition order, drop duplicates.
    seen, routes = set(), []
    for route in found:
        if route not in seen:
            seen.add(route)
            routes.append(route)
    return routes


def api_regex() -> str | None:
    source = PROVISION_SH.read_text(encoding="utf-8")
    match = re.search(r"location ~ \^(/\([^)]*\)(?:\(/\|\$\))?)", source)
    return match.group(1) if match else None


def main() -> int:
    global PROVISION_SH, MAIN_PY
    # Optional overrides so this can be pointed at a candidate config (used by the
    # negative test that proves a bare `location /tts` prefix is reported as a failure).
    if len(sys.argv) > 1:
        import argparse

        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--provision", default=str(PROVISION_SH))
        ap.add_argument("--main", default=str(MAIN_PY))
        args = ap.parse_args()
        PROVISION_SH = Path(args.provision)
        MAIN_PY = Path(args.main)

    if not MAIN_PY.exists() or not PROVISION_SH.exists():
        print(f"FAIL: expected {MAIN_PY} and {PROVISION_SH}")
        return 1

    routes = backend_routes()
    pattern = api_regex()
    if pattern is None:
        print("FAIL: could not find the API `location ~` regex in provision.sh")
        return 1

    try:
        compiled = re.compile(pattern)
    except re.error as err:
        print(f"FAIL: the Nginx regex in provision.sh does not compile: {err}")
        return 1

    provision_source = PROVISION_SH.read_text(encoding="utf-8")

    print(f"  Nginx API regex : {pattern}")
    print(f"  Backend routes  : {len(routes)}")
    print("")

    failures = 0
    for route in routes:
        # Substitute the path params so a real request line can be simulated.
        concrete = re.sub(r"\{[^}]+\}", "TEST", route)

        if route in DEDICATED:
            marker = DEDICATED[route]
            form = dedicated_form(marker, provision_source)
            ok = form == "exact"
            if form == "exact":
                reason = f"dedicated `location = {marker}` block (exact match)"
            elif form == "prefix":
                reason = f"`location {marker}` is a PREFIX -- it shadows {marker}.js"
            else:
                reason = "MISSING dedicated location block"
        else:
            ok = compiled.search(concrete) is not None
            reason = "shared API regex"

        if not ok:
            failures += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {route:<28} via {reason}")
        if not ok:
            print(f"        -> {concrete} would fall through to `location /` and return HTML")

    # The dedicated locations must not be swallowed by the API regex -- but only a PREFIX
    # form is actually at risk.
    #
    # Nginx checks exact (`=`) locations FIRST and stops on a match, before it ever looks at
    # a regex. So `location = /tts` overlapping the API regex is harmless: the exact match
    # always wins. A BARE `location /tts` is a prefix match, and a prefix match loses to the
    # first matching regex -- THAT is the form whose behaviour depends on file order, and the
    # only one worth failing on.
    for route in DEDICATED:
        if (compiled.search(route)
                and dedicated_form(DEDICATED[route], provision_source) == "prefix"):
            failures += 1
            print(f"  FAIL  {route} has a PREFIX location AND matches the API regex")
            print("        -> ambiguous: which wins depends on their order in the file")

    # ---- and the same check for Caddy, which is what actually serves the demo ----
    print("")
    caddy_failures, caddy_total = caddy_check(routes, provision_source)

    print("")
    total = len(routes) + len(DEDICATED) + caddy_total
    failures += caddy_failures
    if failures:
        print(f"{total - failures}/{total} route-to-web-server checks passed. FIX REQUIRED.")
        return 1
    print(f"{total}/{total} route-to-web-server checks passed (Nginx and Caddy).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
