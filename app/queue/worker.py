"""Worker startup boundary: register workflow handlers, then expose the app.

Importing this module (only worker processes do) registers the code-review
handler.  ``app/main.py`` keeps importing ``app.queue.app.procrastinate_app``
directly so the API process registers no handlers.
"""

from __future__ import annotations

from typing import cast

from app.agents.academic_planner.workflow import run_academic_planner
from app.agents.code_review import discovery
from app.agents.code_review.operations import run_code_review_daily
from app.agents.code_review.workflow import run_code_review
from app.queue.app import procrastinate_app
from app.queue.tasks import TaskHandler, register_task_handler

register_task_handler("code_review", run_code_review)
register_task_handler("code_review_daily", run_code_review_daily)
register_task_handler("academic_planner", run_academic_planner)

# Repository discovery is supplied by the account-scale ingestion module when
# enabled.  Importing it lazily keeps the worker boundary usable while a host
# is running only push reviews and daily consolidation.
run_code_review_ingest = cast(
    TaskHandler | None, getattr(discovery, "run_code_review_ingest", None)
)
if run_code_review_ingest is not None:
    register_task_handler("code_review_ingest", run_code_review_ingest)

__all__ = ["procrastinate_app"]
