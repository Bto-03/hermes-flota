"""Test bootstrap: import the plugin directory as the package ``flota_plugin`` (Hermes loads a
directory plugin as a package, so relative imports work). Hermes core must be on PYTHONPATH."""
import importlib.util
import sys
from pathlib import Path

PACKAGE = "flota_plugin"
_ROOT = Path(__file__).resolve().parents[1]
# Keep the plugin root off sys.path so its modules never shadow core ones.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

import ipaddress  # noqa: E402

import pytest  # noqa: E402

# Tests use documentation addresses only (RFC 5737). 203.0.113.0/24 stands in for the
# tailnet (100.64.0.0/10) so no test carries an address from the real Tailscale range.
TEST_TAILNET = ipaddress.ip_network("203.0.113.0/24")


@pytest.fixture(autouse=True)
def _documentation_tailnet(monkeypatch):
    from flota_plugin import detect, pairing
    assert str(detect._TAILNET) == str(pairing._TAILNET) == "100.64.0.0/10"
    monkeypatch.setattr(detect, "_TAILNET", TEST_TAILNET)
    monkeypatch.setattr(pairing, "_TAILNET", TEST_TAILNET)
