"""
Generic parameter loading system for adapters.

Provides profile + override merging without enforcing specific schema.
Each adapter defines its own parameter schema and validators.

Usage:
    from apps.common.adapter.params_loader import ParamsLoader

    # In adapter:
    loader = ParamsLoader(params_dir="./params")
    params = loader.load(profile="fast", overrides={"num_frames": 100})
    # Returns: merged dict (profile + overrides, no defaults added)
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ParamsLoader:
    """
    Generic parameter loading system with profile + override support.

    Features:
    - Load profile from JSON file (e.g., params/fast.json)
    - Merge user overrides on top of profile
    - Deep merge for nested dictionaries
    - No schema enforcement (adapter-specific)
    - No default values added (returns only user-provided params)

    Attributes:
        params_dir: Directory containing profile JSON files

    Example:
        loader = ParamsLoader(params_dir="./params")
        params = loader.load(profile="fast", overrides={"steps": 10})
        # Returns: {"steps": 10, ...fields from fast.json}
    """

    def __init__(self, params_dir: str = "./params"):
        """
        Initialize params loader.

        Args:
            params_dir: Directory containing profile JSON files
        """
        self.params_dir = Path(params_dir).resolve()
        if self.params_dir.exists() and self.params_dir.is_dir():
            logger.info("params: using directory %s", self.params_dir)
        else:
            logger.warning(
                "params: directory %s not found; profile loads may fail",
                self.params_dir,
            )

    @staticmethod
    def deep_merge(base: dict, overrides: dict) -> dict:
        """
        Deep merge two dictionaries (overrides win, nested dicts merged recursively).

        Args:
            base: Base dictionary (e.g., from profile)
            overrides: Override dictionary (e.g., from user request)

        Returns:
            Merged dictionary

        Example:
            base = {"a": 1, "b": {"x": 10}}
            overrides = {"b": {"y": 20}, "c": 3}
            result = deep_merge(base, overrides)
            # {"a": 1, "b": {"x": 10, "y": 20}, "c": 3}
        """
        out = dict(base)
        for k, v in (overrides or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = ParamsLoader.deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    @staticmethod
    def sanitize_profile_name(name: str) -> str:
        """
        Validate profile name (alphanumeric + dash/underscore/dot, max 64 chars).

        Args:
            name: Profile name to validate

        Returns:
            Sanitized profile name

        Raises:
            ValueError: If profile name contains invalid characters

        Example:
            sanitize_profile_name("fast-v2.1")  # OK
            sanitize_profile_name("../../etc/passwd")  # ValueError
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ValueError(f"invalid profile name: {name}")
        return name

    def load_profile_file(self, profile: Optional[str]) -> dict:
        """
        Load profile JSON file from params_dir.

        Args:
            profile: Profile name (e.g., "fast") or None for default.json

        Returns:
            Dictionary loaded from profile JSON

        Raises:
            FileNotFoundError: If profile file doesn't exist
            json.JSONDecodeError: If profile file is invalid JSON

        Example:
            loader.load_profile_file("fast")
            # Loads ./params/fast.json → {"steps": 6, "cfg": 1}
        """
        if not profile:
            path = self.params_dir / "default.json"
        else:
            name = self.sanitize_profile_name(profile)
            path = self.params_dir / f"{name}.json"

        if not path.exists():
            raise FileNotFoundError(f"params profile not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def load(self, profile: Optional[str], overrides: Optional[dict]) -> dict:
        """
        Load parameters: merge profile + overrides (no defaults, no validation).

        Args:
            profile: Profile name (e.g., "fast") or None for no profile
            overrides: User-provided parameter overrides or None

        Returns:
            Merged dictionary (profile + overrides, no defaults added)

        Example:
            # With profile
            loader.load(profile="fast", overrides={"steps": 10})
            # → {..."steps": 10, ...fields from fast.json}

            # Override-only (no profile)
            loader.load(profile=None, overrides={"steps": 10})
            # → {"steps": 10}

            # Profile-only (no overrides)
            loader.load(profile="fast", overrides=None)
            # → {...fields from fast.json}
        """
        if profile:
            logger.debug("params: loading profile=%s", profile)
            base = self.load_profile_file(profile)
        else:
            logger.debug("params: no profile specified, using empty base")
            base = {}

        if overrides:
            logger.debug("params: applying overrides=%s", json.dumps(overrides, ensure_ascii=False))

        merged = self.deep_merge(base, overrides or {})
        logger.debug("params: final merged params=%s", json.dumps(merged, ensure_ascii=False))

        return merged


# Convenience function for backward compatibility
def load_params(
    profile: Optional[str],
    overrides: Optional[dict],
    params_dir: str = "./params"
) -> dict:
    """
    Convenience function: load params with profile + overrides.

    Args:
        profile: Profile name or None
        overrides: Override dict or None
        params_dir: Directory containing profile JSON files

    Returns:
        Merged dictionary

    Example:
        params = load_params(profile="fast", overrides={"steps": 10})
    """
    loader = ParamsLoader(params_dir=params_dir)
    return loader.load(profile, overrides)
