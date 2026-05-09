"""gSage AI — Cost-saving heuristics for the azure_costs tool.

These heuristics complement Azure Advisor's own recommendations with
observations the SDK doesn't surface directly:

- ``oversized_vm`` — VMs whose 14-day average CPU is below a threshold.
- ``idle_vm`` — VMs with very low CPU and network throughput.
- ``stopped_vm_paying_disk`` — Deallocated VMs whose attached managed
  disks are still billable.
- ``orphan_disk`` — Managed disks with no ``managed_by`` owner.
- ``orphan_public_ip`` — Public IPs with no ``ip_configuration``.
- ``orphan_nic`` — NICs with no ``virtual_machine`` attached.
- ``old_snapshots`` — Snapshots older than 90 days.
- ``sku_mismatch`` — VMs where the SKU family is a poor fit for the
  observed CPU pattern (Burstable B-series steady at 100% → suggest D).

Per the v1 design we **never substitute a static pricing table** for
missing live cost data: when ``current_cost_brl`` / ``saving_brl`` is not
available we set those fields to ``None`` and let the agent surface the
recommendation without a savings figure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── Thresholds (overridable via params on the tool side) ───────────────────

OVERSIZED_CPU_AVG_PCT = 20.0           # below this avg over 14d → oversized
OVERSIZED_LOOKBACK_DAYS = 14
IDLE_CPU_AVG_PCT = 1.0
IDLE_NET_BYTES_AVG = 1024              # avg <1KB/s of traffic
IDLE_LOOKBACK_DAYS = 7
OLD_SNAPSHOT_DAYS = 90
SKU_MISMATCH_BURST_AVG_PCT = 90.0      # B-series steady >90% → wrong SKU


# ---------------------------------------------------------------------------
# Recommendation builders (pure functions, no SDK calls — caller fetches data)
# ---------------------------------------------------------------------------


def _rec(
    rec_type: str,
    severity: str,
    resource_id: str,
    resource_name: str,
    *,
    suggested_action: str,
    evidence: dict,
    current_cost_brl: Optional[float] = None,
    saving_brl: Optional[float] = None,
    currency: Optional[str] = None,
) -> dict:
    return {
        "type": rec_type,
        "severity": severity,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "suggested_action": suggested_action,
        "evidence": evidence,
        "current_cost_estimate": current_cost_brl,
        "potential_saving_estimate": saving_brl,
        "currency": currency,
    }


def detect_oversized_vm(
    vm: dict,
    cpu_avg_pct: Optional[float],
    cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
    threshold_pct: float = OVERSIZED_CPU_AVG_PCT,
) -> Optional[dict]:
    """Return a recommendation if the VM is oversized, else ``None``."""
    if cpu_avg_pct is None or cpu_avg_pct >= threshold_pct:
        return None
    name = (vm.get("name") or "").strip()
    rid = (vm.get("id") or "").strip()
    sku = ((vm.get("hardware_profile") or {}).get("vm_size")) or "?"
    saving = round(cost_brl * 0.5, 2) if cost_brl else None
    return _rec(
        rec_type="oversized_vm",
        severity="medium",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"VM '{name}' has avg CPU {cpu_avg_pct:.1f}% over the last "
            f"{OVERSIZED_LOOKBACK_DAYS} days; consider downsizing the "
            f"current SKU '{sku}'."
        ),
        evidence={
            "cpu_avg_pct": round(cpu_avg_pct, 2),
            "lookback_days": OVERSIZED_LOOKBACK_DAYS,
            "current_sku": sku,
            "threshold_pct": threshold_pct,
        },
        current_cost_brl=cost_brl,
        saving_brl=saving,
        currency=currency,
    )


def detect_idle_vm(
    vm: dict,
    cpu_avg_pct: Optional[float],
    net_bytes_avg: Optional[float],
    cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
) -> Optional[dict]:
    if cpu_avg_pct is None or net_bytes_avg is None:
        return None
    if cpu_avg_pct >= IDLE_CPU_AVG_PCT or net_bytes_avg >= IDLE_NET_BYTES_AVG:
        return None
    name = (vm.get("name") or "").strip()
    rid = (vm.get("id") or "").strip()
    saving = cost_brl
    return _rec(
        rec_type="idle_vm",
        severity="high",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"VM '{name}' is effectively idle (CPU {cpu_avg_pct:.2f}%, "
            f"net {net_bytes_avg:.0f}B/s avg over {IDLE_LOOKBACK_DAYS}d); "
            "consider stopping or removing it."
        ),
        evidence={
            "cpu_avg_pct": round(cpu_avg_pct, 3),
            "net_bytes_avg": round(net_bytes_avg, 1),
            "lookback_days": IDLE_LOOKBACK_DAYS,
        },
        current_cost_brl=cost_brl,
        saving_brl=saving,
        currency=currency,
    )


def detect_stopped_vm_paying_disk(
    vm: dict,
    power_state: Optional[str],
    attached_disks: list[dict],
    disk_cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
) -> Optional[dict]:
    if power_state != "deallocated":
        return None
    if not attached_disks:
        return None
    name = (vm.get("name") or "").strip()
    rid = (vm.get("id") or "").strip()
    return _rec(
        rec_type="stopped_vm_paying_disk",
        severity="medium",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"VM '{name}' is deallocated but still billed for "
            f"{len(attached_disks)} attached managed disk(s); review whether "
            "those disks can be removed or the VM deleted."
        ),
        evidence={
            "power_state": power_state,
            "attached_disk_count": len(attached_disks),
            "attached_disks": [
                {
                    "name": d.get("name"),
                    "disk_size_gb": d.get("disk_size_gb"),
                    "sku": (d.get("sku") or {}).get("name"),
                }
                for d in attached_disks[:10]
            ],
        },
        current_cost_brl=disk_cost_brl,
        saving_brl=disk_cost_brl,
        currency=currency,
    )


def detect_orphan_disk(
    disk: dict,
    cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
) -> Optional[dict]:
    if disk.get("managed_by"):
        return None
    name = (disk.get("name") or "").strip()
    rid = (disk.get("id") or "").strip()
    return _rec(
        rec_type="orphan_disk",
        severity="high",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"Disk '{name}' has no owner VM (managedBy is empty); "
            "review and delete or snapshot+delete to stop charges."
        ),
        evidence={
            "disk_size_gb": disk.get("disk_size_gb"),
            "sku": (disk.get("sku") or {}).get("name"),
            "location": disk.get("location"),
            "time_created": disk.get("time_created"),
        },
        current_cost_brl=cost_brl,
        saving_brl=cost_brl,
        currency=currency,
    )


def detect_orphan_public_ip(
    pip: dict,
    cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
) -> Optional[dict]:
    if pip.get("ip_configuration"):
        return None
    sku = (pip.get("sku") or {}).get("name") or "Basic"
    if sku.lower() == "basic":
        # Basic public IPs are free when associated; orphan ones are still
        # safe to flag, but Standard SKUs accrue cost regardless.
        severity = "low"
    else:
        severity = "medium"
    name = (pip.get("name") or "").strip()
    rid = (pip.get("id") or "").strip()
    return _rec(
        rec_type="orphan_public_ip",
        severity=severity,
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"Public IP '{name}' (SKU {sku}) is not associated with any "
            "resource; delete it to stop charges."
        ),
        evidence={
            "sku": sku,
            "allocation_method": pip.get("public_ip_allocation_method"),
            "location": pip.get("location"),
        },
        current_cost_brl=cost_brl,
        saving_brl=cost_brl,
        currency=currency,
    )


def detect_orphan_nic(nic: dict) -> Optional[dict]:
    if nic.get("virtual_machine"):
        return None
    name = (nic.get("name") or "").strip()
    rid = (nic.get("id") or "").strip()
    return _rec(
        rec_type="orphan_nic",
        severity="low",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"NIC '{name}' is not attached to a VM; delete it if no longer "
            "needed."
        ),
        evidence={
            "location": nic.get("location"),
        },
    )


def detect_old_snapshot(
    snapshot: dict,
    cost_brl: Optional[float] = None,
    currency: Optional[str] = None,
    older_than_days: int = OLD_SNAPSHOT_DAYS,
) -> Optional[dict]:
    raw_ts = snapshot.get("time_created")
    if not raw_ts:
        return None
    try:
        if isinstance(raw_ts, str):
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        else:
            ts = raw_ts  # already a datetime
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    age_days = (datetime.now(timezone.utc) - ts).days
    if age_days < older_than_days:
        return None
    name = (snapshot.get("name") or "").strip()
    rid = (snapshot.get("id") or "").strip()
    return _rec(
        rec_type="old_snapshot",
        severity="low",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"Snapshot '{name}' is {age_days} days old; review whether it "
            "is still needed or can be deleted."
        ),
        evidence={
            "age_days": age_days,
            "disk_size_gb": snapshot.get("disk_size_gb"),
            "incremental": snapshot.get("incremental"),
        },
        current_cost_brl=cost_brl,
        saving_brl=cost_brl,
        currency=currency,
    )


def detect_sku_mismatch(
    vm: dict, cpu_avg_pct: Optional[float]
) -> Optional[dict]:
    """Suggest a different SKU family when the workload doesn't fit B-series."""
    sku = ((vm.get("hardware_profile") or {}).get("vm_size")) or ""
    if not sku.lower().startswith(("standard_b", "basic_b")):
        return None
    if cpu_avg_pct is None or cpu_avg_pct < SKU_MISMATCH_BURST_AVG_PCT:
        return None
    name = (vm.get("name") or "").strip()
    rid = (vm.get("id") or "").strip()
    return _rec(
        rec_type="sku_mismatch",
        severity="medium",
        resource_id=rid,
        resource_name=name,
        suggested_action=(
            f"VM '{name}' uses Burstable SKU '{sku}' but runs at "
            f"{cpu_avg_pct:.1f}% CPU on average; consider switching to a "
            "general-purpose D-series SKU to avoid CPU credits "
            "throttling."
        ),
        evidence={
            "current_sku": sku,
            "cpu_avg_pct": round(cpu_avg_pct, 2),
            "threshold_pct": SKU_MISMATCH_BURST_AVG_PCT,
        },
    )


def consolidate_savings(recommendations: list[dict]) -> dict:
    """Aggregate recommendations into a savings summary.

    Recommendations whose ``potential_saving_estimate`` is ``None`` are
    counted but excluded from the monetary total (per the no-static-pricing
    policy).
    """
    by_type: dict[str, dict[str, Any]] = {}
    total_known = 0.0
    total_unknown = 0
    currency: Optional[str] = None
    top_items: list[dict] = []
    for r in recommendations:
        rt = r.get("type") or "unknown"
        bucket = by_type.setdefault(
            rt, {"type": rt, "count": 0, "savings_known": 0.0, "savings_unknown": 0}
        )
        bucket["count"] += 1
        saving = r.get("potential_saving_estimate")
        if saving is None:
            bucket["savings_unknown"] += 1
            total_unknown += 1
        else:
            bucket["savings_known"] += float(saving)
            total_known += float(saving)
            if currency is None and r.get("currency"):
                currency = r.get("currency")
        top_items.append(r)
    top_items.sort(
        key=lambda r: (r.get("potential_saving_estimate") or 0.0), reverse=True
    )
    return {
        "total_potential_savings": round(total_known, 2),
        "currency": currency,
        "recommendations_with_savings": len(recommendations) - total_unknown,
        "recommendations_without_savings": total_unknown,
        "by_type": list(by_type.values()),
        "top_opportunities": top_items[:10],
        "notes": (
            "Recommendations without 'potential_saving_estimate' lack live "
            "Cost Management data; their financial impact is not estimated "
            "(no static pricing fallback)."
            if total_unknown
            else ""
        ),
    }


__all__ = [
    "OLD_SNAPSHOT_DAYS",
    "OVERSIZED_CPU_AVG_PCT",
    "OVERSIZED_LOOKBACK_DAYS",
    "IDLE_CPU_AVG_PCT",
    "IDLE_NET_BYTES_AVG",
    "IDLE_LOOKBACK_DAYS",
    "SKU_MISMATCH_BURST_AVG_PCT",
    "consolidate_savings",
    "detect_idle_vm",
    "detect_old_snapshot",
    "detect_orphan_disk",
    "detect_orphan_nic",
    "detect_orphan_public_ip",
    "detect_oversized_vm",
    "detect_sku_mismatch",
    "detect_stopped_vm_paying_disk",
]


# Suppress "unused import" warnings from very strict checkers
_ = timedelta
