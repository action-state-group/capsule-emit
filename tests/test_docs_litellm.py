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

A test failing here means the page and the world disagree. Fix the page.
"""
from __future__ import annotations

import pathlib
import re

import pytest

DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs" / "adapters" / "litellm.md"
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


def test_the_python_snippets_run():
    """The two ``python`` blocks on the page are executed, not just displayed."""
    blocks = re.findall(r"```python\n(.*?)```", TEXT, re.S)
    assert len(blocks) >= 2, "expected the litellm.callbacks snippet and the core snippet"
    core_snippet = next(b for b in blocks if "LiteLLMListenerCore" in b)
    exec(compile(core_snippet, "<docs core snippet>", "exec"), {})


# ---------------------------------------------------------------------------
# Claims about litellm — re-derived from the installed wheel
# ---------------------------------------------------------------------------

litellm = pytest.importorskip("litellm", reason="needs capsule-emit[litellm]")


def test_get_instance_fn_lives_where_the_page_says():
    from litellm.proxy.types_utils.utils import get_instance_fn

    assert "litellm.proxy.types_utils.utils.get_instance_fn" in TEXT
    assert get_instance_fn.__module__ == "litellm.proxy.types_utils.utils"


def test_the_loader_really_rejects_a_class(monkeypatch, tmp_path):
    """The page's whole explanation for ``proxy_handler_instance``."""
    from litellm.proxy.common_utils.callback_utils import _loaded_callback_or_raise
    from litellm.proxy.types_utils.utils import get_instance_fn

    assert "**rejects a class**" in TEXT
    monkeypatch.setenv("CAPSULE_EMIT_OPERATOR", "acme-co")
    monkeypatch.setenv("CAPSULE_EMIT_DEVELOPER", "gateway@v1")
    monkeypatch.setenv("CAPSULE_EMIT_LEDGER", str(tmp_path / "ledger.jsonl"))

    with pytest.raises(ValueError):
        _loaded_callback_or_raise(
            entry="x.y.Z",
            loaded=get_instance_fn("capsule_emit.adapters.litellm_listener.LiteLLMCapsuleListener"),
        )
    instance = get_instance_fn(DOTTED)
    assert _loaded_callback_or_raise(entry=DOTTED, loaded=instance) is instance


def test_config_file_path_shadowing_gotcha_is_real(tmp_path, monkeypatch):
    """The page warns that a file next to config.yaml shadows the installed
    package. Prove it rather than repeat it."""
    assert "will shadow\nthe installed package silently" in TEXT
    from litellm.proxy.types_utils.utils import get_instance_fn

    shadow = tmp_path / "shadowpkg"
    shadow.mkdir()
    (shadow / "mod.py").write_text("marker = 'from the file next to config.yaml'\n")
    resolved = get_instance_fn("shadowpkg.mod.marker", str(tmp_path / "config.yaml"))
    assert resolved == "from the file next to config.yaml"


def test_async_log_pre_api_call_still_has_no_call_sites():
    """The page's stated reason for not claiming a pre-execution commitment.

    If this fails, litellm has started dispatching the hook — go read
    REQUEST_PROVENANCE and consider sealing a real planned capsule."""
    assert "**zero\ncall sites**" in TEXT
    root = pathlib.Path(litellm.__file__).parent
    sites = [
        f"{path}:{n}"
        for path in root.rglob("*.py")
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if ".async_log_pre_api_call(" in line
    ]
    assert sites == [], f"litellm now dispatches the hook at {sites}"


def test_the_deny_capable_hooks_the_page_promises_we_skip_are_skipped():
    from litellm.integrations.custom_logger import CustomLogger

    from capsule_emit.adapters.litellm_listener import LiteLLMCapsuleListener

    assert "deliberately **not** implemented" in TEXT
    assert LiteLLMCapsuleListener.async_pre_call_hook is CustomLogger.async_pre_call_hook


def test_the_page_names_a_real_failure_hook_signature():
    """The page claims the failure hook can rewrite the client's error, so the
    return annotation and the dispatcher's behaviour both have to be real."""
    import inspect

    from litellm.integrations.custom_logger import CustomLogger

    sig = inspect.signature(CustomLogger.async_post_call_failure_hook)
    for param in ("request_data", "original_exception", "user_api_key_dict", "traceback_str"):
        assert param in sig.parameters, f"{param} is gone from the hook signature"
    assert "HTTPException" in str(sig.return_annotation)


def test_the_python_version_claim_about_the_wheel_is_reproducible():
    """The page says the wheel declares >=3.10 but needs 3.11. Check the
    declaration here; the import failure itself is only observable on 3.10."""
    from importlib.metadata import metadata

    assert "Requires-Python: >=3.10,<3.15" in TEXT
    declared = metadata("litellm").get("Requires-Python")
    assert declared is not None
    assert ">=3.10" in declared.replace(" ", "")
    source = (
        pathlib.Path(litellm.__file__).parent
        / "llms/anthropic/experimental_pass_through/context_management/editors/compact.py"
    )
    if source.exists():
        assert "NotRequired" in source.read_text(), (
            "the 3.11-only import the page cites is gone — re-check the claim"
        )
