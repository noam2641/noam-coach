#!/usr/bin/env python3
"""Generate independent secrets for .env without contacting any service."""

from __future__ import annotations

import secrets

if __name__ == "__main__":
    print(f"HEALTHKIT_API_TOKEN={secrets.token_urlsafe(48)}")
    print(f"MINI_APP_SECRET={secrets.token_urlsafe(48)}")
