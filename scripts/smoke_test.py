#!/usr/bin/env python3
"""Check public liveness/readiness endpoints after deployment."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def fetch(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            payload: object = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        return exc.code, payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    health_status, health = fetch(f"{base}/healthz")
    ready_status, ready = fetch(f"{base}/readyz")
    print("healthz", health_status, health)
    print("readyz", ready_status, ready)
    if health_status != 200 or ready_status != 200:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
