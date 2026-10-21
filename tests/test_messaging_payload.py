from app.messaging.rabbitmq import TaskMessage


def test_task_message_contains_only_task_identifiers_and_dispatch_sequence():
    payload = TaskMessage(task_id="task", doc_id="doc", run_id="run", dispatch_seq=2).to_payload()
    assert payload == {"task_id": "task", "doc_id": "doc", "run_id": "run", "dispatch_seq": 2}
    assert "attempt_count" not in payload and "config" not in payload
