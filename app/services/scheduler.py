"""Due-tick APScheduler for DCA plans (America/New_York).

Feature 1: a single frequent interval job scans all enabled plans via due.due_status.
  - is_due → execute_plan(trigger='scheduled') once for the latest missed slot
  - skip_advance (holiday) → advance last_run_at past the calendar slot, no buy
Per-plan CronTriggers are no longer used (coalesce / catch-up handled by durable last_run_at).
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.config import get_settings
from app.services import dca as dca_service
from app.services.due import due_status
from app.services.cash_monitor import maybe_notify_low_cash

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
DUE_TICK_JOB_ID = "dca_due_tick"


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="America/New_York")
    return _scheduler


def _due_tick() -> None:
    """Scan enabled plans; fire at most one catch-up per plan per tick."""
    try:
        plans = db.list_plans()
    except Exception:
        logger.exception("due_tick: list_plans failed")
        return

    # Optional: surface low-cash warning (debounced) even when nothing is due
    try:
        maybe_notify_low_cash()
    except Exception:
        logger.exception("due_tick: low-cash notify failed")

    for plan in plans:
        if not plan.get("enabled"):
            continue
        plan_id = int(plan["id"])
        try:
            status = due_status(plan)
        except Exception:
            logger.exception("due_tick: due_status failed for plan %s", plan_id)
            continue

        if status.get("skip_advance") and status.get("slot") is not None:
            try:
                dca_service.advance_past_slot(plan, status["slot"])
                logger.info(
                    "due_tick plan %s: holiday skip+advance (%s)",
                    plan_id,
                    status.get("reason"),
                )
            except Exception:
                logger.exception("due_tick: skip+advance failed for plan %s", plan_id)
            continue

        if not status.get("is_due"):
            # Refresh informational next_due_at when cheap
            nxt = status.get("next_due_at")
            if nxt is not None:
                try:
                    iso = nxt.astimezone(__import__("datetime").timezone.utc).isoformat()
                    if (plan.get("next_due_at") or "") != iso:
                        db.set_plan_next_due_at(plan_id, iso)
                except Exception:
                    pass
            continue

        logger.info(
            "due_tick plan %s due (slot=%s reason=%s)",
            plan_id,
            status.get("slot"),
            status.get("reason"),
        )
        try:
            dca_service.execute_plan(plan_id, trigger="scheduled")
        except dca_service.PlanBusyError:
            logger.info("due_tick plan %s busy — will retry next tick", plan_id)
        except (
            dca_service.IdempotencyError,
            dca_service.CapExceededError,
            dca_service.PolicyRejectError,
            dca_service.InsufficientCashError,
        ) as exc:
            logger.warning("due_tick plan %s skipped/refused: %s", plan_id, exc)
        except Exception:
            logger.exception("due_tick: scheduled execute failed for plan %s", plan_id)


def sync_plan_job(plan: dict[str, Any]) -> None:
    """No per-plan cron anymore; due tick covers all enabled plans.

    Kept for call-site compatibility (create/update/toggle). Optionally refresh
    next_due_at cache.
    """
    if not plan:
        return
    try:
        status = due_status(plan)
        nxt = status.get("next_due_at")
        iso = None
        if nxt is not None:
            from datetime import timezone

            iso = nxt.astimezone(timezone.utc).isoformat()
        db.set_plan_next_due_at(int(plan["id"]), iso)
    except Exception:
        logger.exception("sync_plan_job: next_due refresh failed for %s", plan.get("id"))
    # Ensure the interval tick job exists
    _ensure_due_tick_job()


def remove_plan_job(plan_id: int) -> None:
    """No per-plan job to remove; due tick ignores deleted/disabled plans."""
    logger.debug("remove_plan_job(%s): no-op under due-tick model", plan_id)


def _ensure_due_tick_job() -> None:
    sched = get_scheduler()
    settings = get_settings()
    sec = max(60, min(300, int(settings.dca_due_tick_sec or 120)))
    existing = sched.get_job(DUE_TICK_JOB_ID)
    if existing:
        # Recreate if interval drifted
        try:
            trigger = existing.trigger
            if getattr(trigger, "interval", None) is not None:
                if int(trigger.interval.total_seconds()) == sec:
                    return
        except Exception:
            pass
        sched.remove_job(DUE_TICK_JOB_ID)
    sched.add_job(
        _due_tick,
        trigger=IntervalTrigger(seconds=sec, timezone="America/New_York"),
        id=DUE_TICK_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info("Due-tick job scheduled every %ss", sec)


T_TICK_JOB_ID = "t_vwap_tick"


def _t_tick_safe() -> None:
    """Refresh 5m VWAP 做T bars. Failures stay inside this job."""
    try:
        from app.services.t_engine import tick

        tick()
    except Exception:
        logger.exception("t_vwap tick failed")


def _ensure_t_tick_job() -> None:
    """Interval poll for the 做T monitor. Does not touch the DCA due-tick job."""
    sched = get_scheduler()
    settings = get_settings()
    sec = max(5, min(60, int(settings.t_tick_sec or 5)))
    existing = sched.get_job(T_TICK_JOB_ID)
    if existing:
        try:
            trigger = existing.trigger
            if getattr(trigger, "interval", None) is not None:
                if int(trigger.interval.total_seconds()) == sec:
                    return
        except Exception:
            pass
        sched.remove_job(T_TICK_JOB_ID)
    sched.add_job(
        _t_tick_safe,
        trigger=IntervalTrigger(seconds=sec, timezone="America/New_York"),
        id=T_TICK_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=sec,
    )
    logger.info("做T VWAP tick scheduled every %ss", sec)


def reload_all_jobs() -> None:
    sched = get_scheduler()
    for job in list(sched.get_jobs()):
        if str(job.id).startswith("dca_plan_") or str(job.id) == DUE_TICK_JOB_ID:
            sched.remove_job(job.id)
    _ensure_due_tick_job()
    for plan in db.list_plans():
        sync_plan_job(plan)
    try:
        _ensure_t_tick_job()
    except Exception:
        logger.exception("做T tick job was not scheduled")


def start() -> None:
    sched = get_scheduler()
    if not sched.running:
        reload_all_jobs()
        sched.start()
        logger.info("APScheduler started (due-tick mode)")


def shutdown() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
    _scheduler = None
