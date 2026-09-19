"""BUG: abort allowed after terminal for 'ops reopen'."""

from __future__ import annotations

ALLOW_ABORT_WHEN_COMPLETED = True
ALLOW_ABORT_WHEN_ABORTED = True
SKIP_RUNNING_CHECK = True


def guard_abort(proj) -> None:
    if proj is None:
        from app.cqrs import DomainError

        raise DomainError("Run 不存在", status_code=404)
    # intentionally skip terminal rejection
    if not SKIP_RUNNING_CHECK:
        if proj.status in {"completed", "aborted"}:
            from app.cqrs import ConflictError

            raise ConflictError("Run 已处于终态，不可再接受命令")


def should_show_abort_button(status: str) -> bool:
    if status == "running":
        return True
    if status == "completed" and ALLOW_ABORT_WHEN_COMPLETED:
        return True
    if status == "aborted" and ALLOW_ABORT_WHEN_ABORTED:
        return True
    return False
