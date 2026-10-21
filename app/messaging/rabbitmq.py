from dataclasses import asdict, dataclass

from app.db.mysql_models import TaskStatus


@dataclass(frozen=True)
class TaskMessage:
    task_id: str
    doc_id: str
    run_id: str
    dispatch_seq: int = 1

    def to_payload(self):
        return asdict(self)


def should_ack_after_status_persisted(status):
    return status in {
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.WAITING_PUBLISH,
    }
