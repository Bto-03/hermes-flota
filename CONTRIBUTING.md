# Contributing

Thanks for helping. Flota is a small plugin with a strict rule: **standard library only**. The dashboard backend uses the FastAPI that Hermes ships.

## Running the tests

The tests import Hermes core modules, so they need a Hermes checkout on `PYTHONPATH`. No test touches the network, SSH or a real machine.

```sh
git clone https://github.com/Bto-03/hermes-flota flota
git clone https://github.com/NousResearch/hermes-agent hermes-agent

python3 -m venv .venv
. .venv/bin/activate
pip install pytest
pip install -e ./hermes-agent      # Hermes core and its dependencies (FastAPI for the dashboard tests)

cd flota
PYTHONPATH=../hermes-agent python -m pytest -q
```

`pytest.ini` sets `--import-mode=importlib`, and `tests/conftest.py` loads the plugin directory as the package `flota_plugin`, the same way Hermes loads it. The JavaScript tests in `tests/test_desktop.py` and `tests/test_dashboard.py` need `node` and are skipped without it. The dashboard tests are skipped without FastAPI.

Before opening a pull request:

```sh
hermes plugins validate .
```

It should end with `Validation passed.`; the scan must say `safe`; the one expected warning (private core imports) is explained in [docs/security.md](docs/security.md#install-scan). Don't add new ones.

## Guidelines

- Keep changes small and covered by a test. Bug fixes start with a failing test.
- Never put real data in tests, docs or screenshots. Use RFC 5737 documentation addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), names like `desk` / `laptop`, and the user `alice` / `carol`. Build fake SSH keys at test time.
- Anything that runs on a ship must work under `sh`, bash, zsh and fish (no backslashes or single quotes inside remote scripts), and must not prompt.
- Shell-bearing settings (`wake_command`, `sleep_command`, `power_prefix`) must never become settable from the dashboard API.
- Update `docs/` and `CHANGELOG.md` with user-visible changes.

## Reporting security issues

Don't open a public issue; see [SECURITY.md](SECURITY.md).
