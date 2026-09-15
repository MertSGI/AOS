from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_store import RuntimeStore


def test_runtime_store_persists_structured_command_state_events_and_result(tmp_path):
    store = RuntimeStore(tmp_path / "runtime")
    project = ProjectProfile("lari", str(tmp_path / "d"), str(tmp_path), str(tmp_path / "p"))
    command = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=project)
    store.create_command(command.to_dict())
    state = store.read_state(command.command_id)
    assert state["state"] == "QUEUED"
    first = store.append_event(command.command_id, "test.one", {"value": 1})
    second = store.append_event(command.command_id, "test.two", {"value": 2})
    assert second["seq"] == first["seq"] + 1
    events = store.read_events(command.command_id, after_seq=first["seq"])
    assert [e["event_type"] for e in events] == ["test.two"]
    store.write_state(command.command_id, state="RUNNING", worker_pid=123)
    assert store.read_state(command.command_id)["worker_pid"] == 123
