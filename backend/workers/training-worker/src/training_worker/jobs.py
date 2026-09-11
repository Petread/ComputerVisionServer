from __future__ import annotations

from training_worker.runtime import run_training_workflow


def run_training_job(training_job_id: str) -> dict[str, str]:
    return run_training_workflow(training_job_id, resume_requested=False)


def resume_training_job(training_job_id: str) -> dict[str, str]:
    return run_training_workflow(training_job_id, resume_requested=True)
