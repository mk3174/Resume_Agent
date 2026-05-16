"""Workday applier — STUB.

Workday is a SPA with CSRF tokens and dynamic widget IDs. The reliable approach
is to:
  1. Open the apply URL with Patchright, allowing JS to render.
  2. Intercept network calls to `*/wday/cxs/*` to learn the form schema.
  3. POST directly to the same endpoints with extracted CSRF + cookies.

That requires a per-tenant model (each Workday customer's tenant has slightly
different field IDs). Postponed to a follow-up phase.

For now this just parks the application as `needs_review` with a barrier
asking the user to apply manually.
"""

from __future__ import annotations

from apply.base import (
    Applier,
    ApplyContext,
    ApplyResult,
    register,
)
from orchestrator.state import Barrier, BarrierKind, JobSource


class WorkdayApplier(Applier):
    source = JobSource.WORKDAY

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        return ApplyResult(
            submitted=False,
            barriers=[
                Barrier(
                    kind=BarrierKind.UNKNOWN,
                    message="Workday auto-apply is not implemented yet. Please apply manually.",
                    context={"url": str(ctx.job.apply_url)},
                )
            ],
        )


register(WorkdayApplier())
