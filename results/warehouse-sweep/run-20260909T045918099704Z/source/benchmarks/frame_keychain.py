"""Resolve only this benchmark's account-specific macOS Keychain entry."""

from __future__ import annotations

import sys


def keychain_entry(connection_name: str):
    if sys.platform != "darwin":
        raise ValueError("--snowflake-keychain requires macOS Keychain")
    try:
        from keyring.backends.macOS import Keyring
        from snowflake.connector.config_manager import CONFIG_MANAGER
    except ImportError as error:
        raise RuntimeError("Run uv sync --locked --extra snowflake first") from error

    # Use the connector's own profile resolver, including its path/environment rules.
    profiles = CONFIG_MANAGER["connections"]
    if connection_name not in profiles:
        raise ValueError(f"Unknown Snowflake connection: {connection_name}")
    profile = profiles[connection_name]
    account, user = profile.get("account"), profile.get("user")
    if not all(isinstance(value, str) and value.strip() for value in (account, user)):
        raise ValueError("Keychain authentication requires account and user in the profile")
    # Select the native backend explicitly; never fall back to a file-based backend.
    return Keyring(), f"db-performance-comparison.snowflake/{account}", user
