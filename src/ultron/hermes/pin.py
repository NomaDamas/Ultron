"""Pinned upstream Hermes reference metadata."""

from pathlib import Path

HERMES_PINNED_COMMIT = "ee1a744ace44d6ebdda599d0b3a07d0781c1d4cd"
HERMES_REPO_URL = "https://github.com/NousResearch/hermes-agent"
VENDOR_REF_PATH = Path(__file__).resolve().parents[3] / "vendor" / "hermes-agent-ref"


def vendor_present() -> bool:
    """Return whether the pinned vendored Hermes reference is present locally."""
    return VENDOR_REF_PATH.is_dir()
