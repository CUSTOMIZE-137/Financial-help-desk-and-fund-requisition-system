"""Application caching and automatic cache invalidation for high-traffic queries."""

import hashlib
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

USER_VERSION_KEY = "fh_user_cache_ver"
REQ_VERSION_KEY = "fh_req_cache_ver"
DEFAULT_CACHE_TTL = getattr(settings, "CACHE_DEFAULT_TIMEOUT", 300)


def _get_version(key: str) -> int:
    """Read or initialize a monotonic generation counter in the cache backend."""
    val = cache.get(key)
    if val is None:
        cache.set(key, 1, timeout=None)
        return 1
    return int(val)


def _bump_version(key: str) -> int:
    """Atomically increment a cache generation key so dependent entries expire immediately."""
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 2, timeout=None)
        return 2


def get_user_cache_version() -> int:
    """Return the current generation counter for user/delegation cache keys."""
    return _get_version(USER_VERSION_KEY)


def bump_user_cache_version() -> int:
    """Invalidate cached delegation and role-navigation lookups."""
    return _bump_version(USER_VERSION_KEY)


def get_requisition_cache_version() -> int:
    """Return the current generation counter for requisition summary cache keys."""
    return _get_version(REQ_VERSION_KEY)


def bump_requisition_cache_version() -> int:
    """Invalidate cached dashboard counts and requisition summaries."""
    return _bump_version(REQ_VERSION_KEY)


def get_cached_delegate_roles(user, fetch_fn: Callable[[], list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """Return active delegate (role, department) pairs using per-request and shared cache."""
    if not getattr(user, "is_authenticated", False) or not user.pk:
        return []
    today = timezone.localdate().isoformat()
    u_ver = get_user_cache_version()
    memo_stamp = getattr(user, "_cached_delegate_stamp", None)
    if memo_stamp == (u_ver, today) and hasattr(user, "_cached_delegate_roles"):
        return user._cached_delegate_roles

    cache_key = f"fh:delegates:v{u_ver}:u{user.pk}:{today}"
    roles = cache.get(cache_key)
    if roles is None:
        roles = fetch_fn()
        cache.set(cache_key, roles, timeout=DEFAULT_CACHE_TTL)

    user._cached_delegate_stamp = (u_ver, today)
    user._cached_delegate_roles = roles
    return roles


def get_cached_dashboard_counts(user, fetch_fn: Callable[[], dict[str, int]]) -> dict[str, int]:
    """Cache role-scoped dashboard summary counts until any requisition or user changes."""
    u_ver = get_user_cache_version()
    r_ver = get_requisition_cache_version()
    today = timezone.localdate().isoformat()
    raw_id = f"{u_ver}:{r_ver}:{today}:{user.pk}:{user.role}:{user.department}:{user.is_superuser}"
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:20]
    cache_key = f"fh:dash_counts:{digest}"
    counts = cache.get(cache_key)
    if counts is None:
        counts = fetch_fn()
        cache.set(cache_key, counts, timeout=DEFAULT_CACHE_TTL)
    return counts


def get_cached_requisition_pdf(requisition, build_fn: Callable[[], bytes]) -> bytes:
    """Cache generated PDF bytes keyed by the requisition's state, items, logs, and signatures."""
    r_ver = get_requisition_cache_version()
    u_ver = get_user_cache_version()
    updated_ts = int(requisition.updated_at.timestamp()) if requisition.updated_at else 0
    sig_name = getattr(requisition.requestor.signature_image, "name", "") or ""
    fingerprint = f"{r_ver}:{u_ver}:{requisition.pk}:{requisition.status}:{updated_ts}:{requisition.amount}:{sig_name}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    cache_key = f"fh:req_pdf:{requisition.pk}:{digest}"
    pdf_bytes = cache.get(cache_key)
    if pdf_bytes is None:
        pdf_bytes = build_fn()
        cache.set(cache_key, pdf_bytes, timeout=3600)
    return pdf_bytes


def _on_user_changed(sender, instance, **kwargs):
    if hasattr(instance, "_cached_delegate_roles"):
        delattr(instance, "_cached_delegate_roles")
    bump_user_cache_version()


def _on_requisition_changed(sender, instance, **kwargs):
    bump_requisition_cache_version()


def register_cache_signals():
    """Connect model save/delete signals to cache version invalidation."""
    from requisitions.models import (
        AccountabilitySubmission,
        Requisition,
        RequisitionItem,
        RequisitionLog,
        User,
    )

    post_save.connect(_on_user_changed, sender=User, dispatch_uid="fh_user_save_cache")
    post_delete.connect(_on_user_changed, sender=User, dispatch_uid="fh_user_del_cache")

    for model_cls, name in (
        (Requisition, "req"),
        (RequisitionItem, "reqitem"),
        (RequisitionLog, "reqlog"),
        (AccountabilitySubmission, "acct"),
    ):
        post_save.connect(
            _on_requisition_changed,
            sender=model_cls,
            dispatch_uid=f"fh_{name}_save_cache",
        )
        post_delete.connect(
            _on_requisition_changed,
            sender=model_cls,
            dispatch_uid=f"fh_{name}_del_cache",
        )
