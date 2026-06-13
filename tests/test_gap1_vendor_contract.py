from pathlib import Path

import pytest


def vendor_present() -> bool:
    return Path("/Ultron/vendor/hermes-agent").is_dir() or Path("vendor/hermes-agent").is_dir()


def test_vendored_symbol_contract_skips_without_vendor():
    if not vendor_present():
        pytest.skip("vendored hermes-agent reference is absent in this sandbox")
    import hermes  # type: ignore[import-not-found]

    assert hermes is not None
