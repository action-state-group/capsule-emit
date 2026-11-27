# SPDX-License-Identifier: Apache-2.0
"""Claims in the LiteLLM docs page about litellm itself, re-derived from the
installed wheel. The claims about capsule-emit are checked in
``test_docs_litellm.py``.
"""
from __future__ import annotations

import pathlib

import pytest
from test_docs_litellm import (  # helpers shared with the framework-free tests
    DOTTED,
    TEXT,
)

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
