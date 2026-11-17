from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from tender_system.operator.web_matching import execute_matching


def test_manual_matching_enqueues_reused_results(monkeypatch):
    session = object()
    runtime = SimpleNamespace(
        sessions=SimpleNamespace(begin=lambda: nullcontext(session)),
        config=object(),
        clock=SimpleNamespace(now=lambda: "now"),
    )
    runtime.sessions = Sessions(session)
    match = object()
    enqueue = Mock()
    monkeypatch.setattr("tender_system.operator.web_matching.validate_matching_execution", Mock())
    monkeypatch.setattr(
        "tender_system.operator.automatic_matching.run_jobs",
        Mock(return_value=({"reused": 1}, "completed")),
    )
    monkeypatch.setattr(
        "tender_system.persistence.matching_storage.SqlMatchJobRepository",
        lambda s: SimpleNamespace(results=lambda **kw: (match,)),
    )
    monkeypatch.setattr("tender_system.operator.telegram.enqueue_notification", enqueue)
    result = execute_matching(runtime, {"operation_id": str(uuid4()), "job_ids": [str(uuid4())]})
    assert result == ({"reused": 1}, "completed")
    enqueue.assert_called_once_with(session, match, "now")


class Sessions:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return nullcontext(self.session)

    def begin(self):
        return nullcontext(self.session)
