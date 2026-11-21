# SPDX-License-Identifier: Apache-2.0
"""task_seal_request: structure by default, full text only when asked."""

from __future__ import annotations

import json

import pytest

from capsule_emit.cli import main
from capsule_emit.task_request import TaskRequestError, task_seal_request

CONVO = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "yes"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "pay", "arguments": '{"to": "bob", "amount": 2.5}'}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "done"},
]
KW = {"suite": "my-evals", "domain": "po-agent", "task_id": "t1", "operator": "acme", "developer": "po@v1"}


def build(**extra):
    return task_seal_request(CONVO, timestamp="2026-11-20T10:00:00Z", **KW, **extra)


def test_structure_mode_seals_no_text_names_arguments_or_digests():
    req = build()
    inter = req["payload"]["agent_interaction"]
    assert inter["mode"] == "structure"
    assert inter["messages"] == [
        {"role": "system", "nonempty": True},
        {"role": "user", "nonempty": True},
        {"role": "assistant", "nonempty": False, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "nonempty": True, "tool_call_id": "c1"},
        {"role": "assistant", "nonempty": True},
    ]
    dumped = json.dumps(inter)
    for secret in ("yes", "bob", "pay", "be brief", "done", "sha256"):
        assert secret not in dumped


def test_request_shape_and_case():
    req = build()
    assert req["spec_version"] == "capsule-seal-request/v1"
    assert req["capsule"] == {
        "ActionID": "urn:agent-task:my-evals:po-agent:task-t1:trial-0",
        "ActionType": "fyi",
        "Operator": "acme",
        "Developer": "po@v1",
        "Timestamp": "2026-11-20T10:00:00Z",
    }
    assert req["payload"]["case"] == {"benchmark": "my-evals", "domain": "po-agent", "task_id": "t1", "trial": 0}


def test_full_text_mode_states_its_mode_and_keeps_text_and_floats_as_strings():
    inter = build(full_text=True)["payload"]["agent_interaction"]
    assert inter["mode"] == "full_text"
    assert inter["messages"][1]["content"] == "yes"
    assert inter["messages"][2]["tool_calls"] == [{"id": "c1", "name": "pay", "arguments": '{"to": "bob", "amount": 2.5}'}]


def test_full_text_floats_in_dict_arguments_become_strings():
    convo = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "a", "name": "f", "arguments": {"q": 2.5}}]},
    ]
    req = task_seal_request(convo, full_text=True, timestamp="2026-11-20T10:00:00Z", **KW)
    assert req["payload"]["agent_interaction"]["messages"][1]["tool_calls"][0]["arguments"] == {"q": "2.5"}


def test_incomplete_conversation_is_built_as_it_happened():
    convo = [{"role": "user", "content": "go"}, {"role": "assistant", "tool_calls": [{"id": "a", "name": "f"}]}]
    msgs = task_seal_request(convo, timestamp="2026-11-20T10:00:00Z", **KW)["payload"]["agent_interaction"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_same_input_same_request():
    assert build() == build()


@pytest.mark.parametrize(
    "convo",
    [
        [],
        ["x"],
        [{"role": "robot", "content": "x"}],
        [{"role": "user", "tool_calls": [{"id": "a"}]}],
        [{"role": "assistant", "tool_calls": [{"name": "f"}]}],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": "x", "tool_call_id": "a"}],
    ],
)
def test_bad_conversation_refused(convo):
    with pytest.raises(TaskRequestError):
        task_seal_request(convo, timestamp="2026-11-20T10:00:00Z", **KW)


@pytest.mark.parametrize(
    "bad",
    [{"suite": "a:b"}, {"domain": ""}, {"task_id": ""}, {"trial": -1}, {"trial": True}, {"operator": ""}, {"timestamp": "yesterday"}, {"timestamp": "2026-11-20T10:00:00"}],
)
def test_bad_identity_or_time_refused(bad):
    kw = {**KW, "timestamp": "2026-11-20T10:00:00Z", **bad}
    with pytest.raises(TaskRequestError):
        task_seal_request(CONVO, **kw)


def test_cli_writes_request(tmp_path, capsys):
    src, out = tmp_path / "c.json", tmp_path / "req.json"
    src.write_text(json.dumps({"messages": CONVO}))
    argv = ["task-request", "--messages", str(src), "--suite", "my-evals", "--domain", "po-agent", "--task-id", "t1",
            "--operator", "acme", "--developer", "po@v1", "--timestamp", "2026-11-20T10:00:00Z", "-o", str(out)]
    assert main(argv) == 0
    assert json.loads(out.read_text()) == build()
    assert "mode structure" in capsys.readouterr().out
    assert main([*argv, "--full-text"]) == 0
    assert json.loads(out.read_text())["payload"]["agent_interaction"]["mode"] == "full_text"


def test_cli_refuses_bad_input(tmp_path, capsys):
    src = tmp_path / "c.json"
    src.write_text("[]")
    argv = ["task-request", "--messages", str(src), "--suite", "s", "--domain", "d", "--task-id", "t",
            "--operator", "o", "--developer", "v", "-o", str(tmp_path / "r.json")]
    assert main(argv) == 1
    assert "no messages" in capsys.readouterr().err
    assert main(["task-request", "--messages", str(tmp_path / "missing.json"), *argv[3:]]) == 1
    assert not (tmp_path / "r.json").exists()
