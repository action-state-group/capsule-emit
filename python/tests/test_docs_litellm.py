# SPDX-License-Identifier: Apache-2.0
"""Docs gate for docs/adapters/litellm.md.

The adapter page makes claims about *someone else's* released package. Those are
the claims most likely to rot silently: litellm ships ~1000 releases, and a page
that says "the loader rejects a class" is worthless the day it stops being true.

So every load-bearing claim on that page is re-derived here from the installed
artifacts rather than asserted in prose:

- every symbol, env var and config key the page names exists
- the ``config.yaml`` snippet parses and its dotted string resolves
- litellm's loader really does reject the class and accept the instance
- ``async_log_pre_api_call`` really has no call sites (the page's reason for
  not claiming a pre-execution commitment)
- the pinned version in the page matches the pinned version in pyproject.toml
- the allowlists quoted in the page are the allowlists in the code

Claims that need the installed litellm wheel are in ``test_docs_litellm_sdk.py``.

A test failing here means the page and the world disagree. Fix the page.
"""
from __future__ import annotations

import pathlib
import re

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "adapters" / "litellm.md"
PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
TEXT = DOCS.read_text()

DOTTED = "capsule_emit.adapters.litellm_listener.proxy_handler_instance"


def test_the_page_exists_and_is_linked_from_the_adapter_index():
    index = (DOCS.parent / "README.md").read_text()
    assert "litellm" in index.lower(), "docs/adapters/README.md does not link the page"


# ---------------------------------------------------------------------------
# Claims about our own code
# ---------------------------------------------------------------------------


def test_every_symbol_the_page_names_exists():
    from capsule_emit.adapters import litellm_listener as mod

    for symbol in ("LiteLLMCapsuleListener", "LiteLLMListenerCore", "listener_from_env"):
        assert symbol in TEXT, f"page no longer mentions {symbol}"
        assert hasattr(mod, symbol), f"{symbol} is documented but missing"


def test_every_env_var_the_page_documents_is_read_by_the_code():
    from capsule_emit.adapters import litellm_listener as mod

    source = pathlib.Path(mod.__file__).read_text()
    documented = set(re.findall(r"`(CAPSULE_EMIT_[A-Z_]+)`", TEXT))
    assert documented, "the page documents no environment variables — did the table move?"
    for var in documented:
        assert var in source, f"{var} is documented but never read"


def test_the_allowlists_in_the_page_match_the_code():
    from capsule_emit.adapters.litellm_listener import (
        _PROMPT_FIELDS,
        _REQUEST_FIELDS,
        _RESPONSE_FIELDS,
    )

    request_line = next(ln for ln in TEXT.splitlines() if ln.startswith("- request:"))
    response_line = next(ln for ln in TEXT.splitlines() if ln.startswith("- response:"))
    for field in _REQUEST_FIELDS:
        assert f"`{field}`" in request_line, f"{field} sealed but not documented"
    for field in _RESPONSE_FIELDS:
        assert f"`{field}`" in response_line, f"{field} sealed but not documented"
    # the page states the prompt-field preference order explicitly
    assert "`messages`, `input`, `prompt`" in TEXT
    assert _PROMPT_FIELDS == ("messages", "input", "prompt")


def test_the_page_names_the_forbidden_params_and_they_are_still_forbidden():
    from capsule_emit.adapters.litellm_listener import (
        _PROMPT_FIELDS,
        _REQUEST_FIELDS,
        _RESPONSE_FIELDS,
    )

    assert "`litellm_params` and `optional_params` are not on it" in TEXT
    every = set(_REQUEST_FIELDS) | set(_PROMPT_FIELDS) | set(_RESPONSE_FIELDS)
    assert "litellm_params" not in every
    assert "optional_params" not in every


def test_the_pinned_version_agrees_with_pyproject():
    pinned = re.search(r"`litellm==([0-9.]+)` wheel", TEXT)
    assert pinned, "the page no longer states which release it was verified against"
    floor = re.search(r'litellm = \["litellm>=([0-9.]+)"\]', PYPROJECT.read_text())
    assert floor, "pyproject has no [litellm] extra"
    assert pinned.group(1) == floor.group(1), (
        f"page says {pinned.group(1)}, pyproject pins {floor.group(1)}"
    )


def test_the_demo_the_page_tells_you_to_run_exists():
    root = DOCS.resolve().parents[2]
    assert "python examples/litellm-listener/demo.py" in TEXT
    assert (root / "examples" / "litellm-listener" / "demo.py").exists()


def test_the_config_snippet_parses_and_names_the_dotted_path():
    block = re.search(r"```yaml\n(.*?)```", TEXT, re.S)
    assert block, "the config.yaml snippet is gone"
    body = block.group(1)
    assert "litellm_settings:" in body
    assert f'callbacks: ["{DOTTED}"]' in body


def test_the_python_snippets_run(tmp_path, monkeypatch):
    """The two ``python`` blocks on the page are executed, not just displayed."""
    monkeypatch.chdir(tmp_path)  # the snippet writes the default ledger in the cwd
    blocks = re.findall(r"```python\n(.*?)```", TEXT, re.S)
    assert len(blocks) >= 2, "expected the litellm.callbacks snippet and the core snippet"
    core_snippet = next(b for b in blocks if "LiteLLMListenerCore" in b)
    exec(compile(core_snippet, "<docs core snippet>", "exec"), {})
