import json
from typing import Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import tasks_v2

from app.core.errors import RetryableDependencyError
from app.models.api import ProcessJobRequest


class FinalizationQueue(Protocol):
    def enqueue_finalize_output(self, payload: ProcessJobRequest) -> None:
        """Enqueue CPU output encoding for a completed model-output object."""


class InMemoryFinalizationQueue:
    def __init__(self) -> None:
        self.payloads: list[ProcessJobRequest] = []
        self._job_ids: set[str] = set()

    def enqueue_finalize_output(self, payload: ProcessJobRequest) -> None:
        if payload.job_id in self._job_ids:
            return
        self.payloads.append(payload)
        self._job_ids.add(payload.job_id)


class CloudTasksFinalizationQueue:
    def __init__(
        self,
        project_id: str,
        location: str,
        queue_name: str,
        service_url: str,
        service_account_email: str,
        audience: str,
    ) -> None:
        self._client = tasks_v2.CloudTasksClient()
        self._parent = self._client.queue_path(project_id, location, queue_name)
        self._service_url = service_url.rstrip("/")
        self._service_account_email = service_account_email
        self._audience = audience

    def enqueue_finalize_output(self, payload: ProcessJobRequest) -> None:
        task_id = f"finalize-output-{payload.job_id}"
        task = {
            "name": f"{self._parent}/tasks/{task_id}",
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self._service_url}/tasks/finalize-output",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload.model_dump(exclude_none=True)).encode(),
                "oidc_token": {
                    "service_account_email": self._service_account_email,
                    "audience": self._audience,
                },
            },
        }
        try:
            self._client.create_task(parent=self._parent, task=task)
        except google_exceptions.AlreadyExists:
            return
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError(
                "queue_unavailable", "Task queue is unavailable."
            ) from exc


_RETRYABLE_GOOGLE_EXCEPTIONS = (
    google_exceptions.Aborted,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.TooManyRequests,
)
