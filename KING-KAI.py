#!/usr/bin/env python3
"""
👑 KING KAI — CloudCostChefs: OCI Forgotten Resource Detective (Python Edition)

Sniffs out forgotten cloud resources in your OCI tenancy—no manual sleuthing required.

Scans every compartment (including root) and flags:
  • Orphaned Block Volumes (no attachments)
  • Unattached Reserved Public IPs (REGION + AD scopes)
  • Empty Network Security Groups (NSGs with zero VNICs)
  • Load Balancers with no backends (validated via list_backends)
  • Old-gen Compute instances (shape matches regex)
  • Resources with absolutely NO tags
  • Resources with sketchy names (test|temp|demo|old|backup|poc)

PLUS (KING KAI Upgrade Advisor):
  • When old shapes are found, shows:
      - counts (AMD E2/E3/E4 + Intel Standard1/2, including zeros)
      - recommended upgrade targets:
          AMD  -> VM.Standard.E5.Flex and/or VM.Standard.E6.Flex
          Intel-> VM.Standard3.Flex and VM.Optimized3.Flex
      - per-AD shape catalog availability (✅/❌) and E5/E6 series in catalog
      - tenancy limits snapshot (including explicit 0) + available/used when supported
  • Included in the HTML report (very important), and also printed to console.

Usage:
  python oci_forgotten_resources_king_kai.py \
    --profile DEFAULT \
    --output-html forgotten_resources_report.html \
    --output-csv forgotten_resources_report.csv \
    --old-shape-pattern "VM\\.(Standard1|Standard2).*"
"""

import oci
import argparse
import csv
import re
import sys
import html as htmlmod
from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set

# ------------------------------------------------------------
#  🍜 Helper: Gather all compartments (root + active children) + names
# ------------------------------------------------------------
def collect_all_compartments(identity_client, tenancy_id: str):
    """
    Returns:
      - compartment_ids: [tenancy_id, <all active sub-compartment OCIDs>]
      - comp_name_by_id: { ocid: display_name }
    """
    comp_ids: List[str] = []
    comp_name_by_id: Dict[str, str] = {}

    # All ACTIVE compartments under tenancy (recursive)
    all_response = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        compartment_id=tenancy_id,
        compartment_id_in_subtree=True,
        lifecycle_state="ACTIVE",
    )

    for cp in all_response.data:
        comp_ids.append(cp.id)
        comp_name_by_id[cp.id] = getattr(cp, "name", cp.id)

    # Root tenancy itself
    try:
        tenancy = identity_client.get_tenancy(tenancy_id).data
        comp_name_by_id[tenancy_id] = getattr(tenancy, "name", "tenancy-root")
    except Exception:
        comp_name_by_id[tenancy_id] = "tenancy-root"

    comp_ids.append(tenancy_id)
    return comp_ids, comp_name_by_id


# ------------------------------------------------------------
#  🍳 Helper: Check for ZERO tags
# ------------------------------------------------------------
def has_no_tags(resource) -> bool:
    ff = getattr(resource, "freeform_tags", None)
    df = getattr(resource, "defined_tags", None)
    return (not ff or len(ff) == 0) and (not df or len(df) == 0)


def esc(s: Any) -> str:
    """Safe HTML escaping."""
    if s is None:
        return ""
    return htmlmod.escape(str(s), quote=True)


# ------------------------------------------------------------
#  👑 KING KAI — Upgrade Advisor helpers
# ------------------------------------------------------------
AMD_UPGRADE_TARGETS = ["VM.Standard.E5.Flex", "VM.Standard.E6.Flex"]
INTEL_UPGRADE_TARGETS = ["VM.Standard3.Flex", "VM.Optimized3.Flex"]

# Limit names used by OCI Limits service (typical programmatic names)
LIMITS_BY_TARGET = {
    "VM.Standard.E5.Flex": ["standard-e5-core-count", "standard-e5-memory-count"],
    "VM.Standard.E6.Flex": ["standard-e6-core-count", "standard-e6-memory-count"],
    "VM.Standard3.Flex":   ["standard3-core-count", "standard3-memory-count"],
    "VM.Optimized3.Flex":  ["optimized3-core-count", "optimized3-memory-count"],
}


def classify_old_shape(shape: str) -> str:
    """
    Classify old shape families for reporting.
    """
    if re.match(r"^VM\.Standard\.E2\b", shape): return "AMD_E2"
    if re.match(r"^VM\.Standard\.E3\b", shape): return "AMD_E3"
    if re.match(r"^VM\.Standard\.E4\b", shape): return "AMD_E4"
    if re.match(r"^VM\.Standard1\b", shape):    return "INTEL_STD1"
    if re.match(r"^VM\.Standard2\b", shape):    return "INTEL_STD2"
    return "OTHER"


def discover_compute_service_name(limits_client, tenancy_id: str) -> str:
    """
    Limits service uses a service_name string (often 'compute').
    We attempt to discover it, defaulting safely to 'compute'.
    """
    try:
        svcs = oci.pagination.list_call_get_all_results(
            limits_client.list_services,
            compartment_id=tenancy_id
        ).data

        for s in svcs:
            name = (getattr(s, "name", None) or getattr(s, "service_name", None) or "")
            desc = (getattr(s, "description", None) or getattr(s, "friendly_name", None) or "")
            if str(name).lower() == "compute":
                return str(name)
            if "compute" in str(desc).lower():
                return str(name) if name else "compute"
    except Exception:
        pass
    return "compute"


def build_limit_value_index(limits_client, tenancy_id: str, service_name: str):
    """
    index[(limit_name, scope_type, availability_domain)] = value
    """
    index: Dict[tuple, Any] = {}
    try:
        vals = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=tenancy_id,
            service_name=service_name
        ).data

        for lv in vals:
            key = (
                getattr(lv, "name", None),
                getattr(lv, "scope_type", None),
                getattr(lv, "availability_domain", None),
            )
            index[key] = getattr(lv, "value", None)
    except Exception:
        pass
    return index


def get_resource_availability_safe(
    limits_client,
    service_name: str,
    tenancy_id: str,
    limit_name: str,
    ad: Optional[str]
):
    """
    Try to get (available, used) via get_resource_availability.
    Not all limits support it; may raise ServiceError.
    """
    # Try REGION first
    try:
        ra = limits_client.get_resource_availability(
            service_name=service_name,
            limit_name=limit_name,
            compartment_id=tenancy_id
        ).data
        return {
            "available": getattr(ra, "available", None),
            "used": getattr(ra, "used", None),
            "effective_quota_value": getattr(ra, "effective_quota_value", None),
        }
    except oci.exceptions.ServiceError:
        # Try AD-scoped if AD provided
        if ad:
            try:
                ra = limits_client.get_resource_availability(
                    service_name=service_name,
                    limit_name=limit_name,
                    compartment_id=tenancy_id,
                    availability_domain=ad
                ).data
                return {
                    "available": getattr(ra, "available", None),
                    "used": getattr(ra, "used", None),
                    "effective_quota_value": getattr(ra, "effective_quota_value", None),
                }
            except oci.exceptions.ServiceError:
                return None
        return None


def list_shapes_in_ad(compute_client, tenancy_id: str, ad: str) -> Set[str]:
    """
    Returns set of shape names offered in a given AD (catalog availability, not capacity guarantee).
    """
    try:
        shapes = oci.pagination.list_call_get_all_results(
            compute_client.list_shapes,
            compartment_id=tenancy_id,
            availability_domain=ad
        ).data
        return {getattr(s, "shape", "") for s in shapes if getattr(s, "shape", "")}
    except Exception:
        return set()


def compute_upgrade_advice(
    old_gen_tracker: List[Dict[str, Any]],
    compute_client,
    limits_client,
    tenancy_id: str,
    old_shape_pattern: str
) -> Dict[str, Any]:
    """
    Computes KING KAI upgrade advice as a structured dict (for console + HTML).
    """
    counts = Counter()
    ads: Set[str] = set()

    for row in old_gen_tracker:
        shape = row.get("shape", "") or ""
        ad = row.get("availability_domain")
        if ad:
            ads.add(ad)
        counts[classify_old_shape(shape)] += 1

    # force explicit zeros
    for k in ["AMD_E2", "AMD_E3", "AMD_E4", "INTEL_STD1", "INTEL_STD2"]:
        counts.setdefault(k, 0)

    amd_present = (counts["AMD_E2"] + counts["AMD_E3"] + counts["AMD_E4"]) > 0
    intel_present = (counts["INTEL_STD1"] + counts["INTEL_STD2"]) > 0

    targets: List[str] = []
    if amd_present:
        targets.extend(AMD_UPGRADE_TARGETS)
    if intel_present:
        targets.extend(INTEL_UPGRADE_TARGETS)

    advice: Dict[str, Any] = {
        "pattern": old_shape_pattern,
        "counts": dict(counts),
        "amd_present": amd_present,
        "intel_present": intel_present,
        "targets": targets,
        "ads": sorted([a for a in ads if a]),
        "catalog": {},        # per AD
        "limits": [],         # list of limit rows
        "compute_service": "",# discovered service name
    }

    if not targets:
        return advice

    # Catalog availability per AD
    for ad in advice["ads"]:
        shape_set = list_shapes_in_ad(compute_client, tenancy_id, ad)
        advice["catalog"][ad] = {
            "targets": {t: (t in shape_set) for t in targets},
            "e5_series": sorted([s for s in shape_set if s.startswith("VM.Standard.E5")]),
            "e6_series": sorted([s for s in shape_set if s.startswith("VM.Standard.E6")]),
        }

    # Limits snapshot (include explicit 0)
    service_name = discover_compute_service_name(limits_client, tenancy_id)
    advice["compute_service"] = service_name
    limit_index = build_limit_value_index(limits_client, tenancy_id, service_name)

    needed_limits: Set[str] = set()
    for t in targets:
        needed_limits.update(LIMITS_BY_TARGET.get(t, []))
    needed_limits_sorted = sorted(needed_limits)

    any_ad = advice["ads"][0] if advice["ads"] else None

    for limit_name in needed_limits_sorted:
        region_val = None
        global_val = None
        ad_vals: Dict[str, Any] = {}

        for (lname, scope, ad), val in limit_index.items():
            if lname != limit_name:
                continue
            if scope == "REGION":
                region_val = val
            elif scope == "GLOBAL":
                global_val = val
            elif scope == "AD":
                ad_vals[str(ad)] = val

        ra = get_resource_availability_safe(
            limits_client=limits_client,
            service_name=service_name,
            tenancy_id=tenancy_id,
            limit_name=limit_name,
            ad=any_ad
        ) or {}

        advice["limits"].append({
            "name": limit_name,
            "global": global_val,
            "region": region_val,
            "ad_vals": {k: ad_vals[k] for k in sorted(ad_vals.keys())} if ad_vals else {},
            "availability": ra,
        })

    return advice


def render_upgrade_advice_html(advice: Dict[str, Any]) -> str:
    """
    Render KING KAI Upgrade Advisor block as HTML.
    Explicitly shows zeros (e.g., REGION limit=0).
    """
    if not advice:
        return ""

    c = advice.get("counts", {}) or {}
    amd_line = f"E2={c.get('AMD_E2', 0)} | E3={c.get('AMD_E3', 0)} | E4={c.get('AMD_E4', 0)}"
    intel_line = f"Standard1={c.get('INTEL_STD1', 0)} | Standard2={c.get('INTEL_STD2', 0)}"
    pattern = esc(advice.get("pattern", ""))
    targets: List[str] = advice.get("targets", []) or []
    ads: List[str] = advice.get("ads", []) or []
    service_name = esc(advice.get("compute_service", ""))

    def fmt(v):
        # Keep 0 visible; hide only None
        return "" if v is None else esc(v)

    block: List[str] = []
    block.append('<div class="kingsai">')
    block.append('<h2>👑 KING KAI — Upgrade Advisor</h2>')
    block.append(f'<p><strong>Old-shape pattern:</strong> <code>{pattern}</code></p>')

    block.append('<div class="kpi-row">')
    block.append(f'<div class="kpi"><div class="kpi-title">AMD old instances</div><div class="kpi-val">{esc(amd_line)}</div></div>')
    block.append(f'<div class="kpi"><div class="kpi-title">Intel old instances</div><div class="kpi-val">{esc(intel_line)}</div></div>')
    block.append('</div>')

    if not targets:
        block.append('<p><em>No AMD E2/E3/E4 or Intel Standard1/2 shapes were detected. Upgrade targets not applicable.</em></p>')
        block.append('</div>')
        return "\n".join(block)

    block.append('<h3>Recommended upgrade targets</h3>')
    block.append('<ul>')
    if advice.get("amd_present"):
        block.append(f"<li><strong>AMD →</strong> {', '.join(map(esc, AMD_UPGRADE_TARGETS))}</li>")
    if advice.get("intel_present"):
        block.append(f"<li><strong>Intel →</strong> {', '.join(map(esc, INTEL_UPGRADE_TARGETS))}</li>")
    block.append('</ul>')

    # Catalog per AD
    if ads:
        block.append('<h3>Shape catalog availability (per AD)</h3>')
        block.append('<table class="advisor-table">')
        block.append('<tr><th>Availability Domain</th>' + "".join([f"<th>{esc(t)}</th>" for t in targets]) +
                     '<th>E5 series (catalog)</th><th>E6 series (catalog)</th></tr>')

        for ad in ads:
            entry = (advice.get("catalog", {}) or {}).get(ad, {}) or {}
            tgt_map = entry.get("targets", {}) or {}
            e5 = ", ".join(entry.get("e5_series", []) or []) or "(none)"
            e6 = ", ".join(entry.get("e6_series", []) or []) or "(none)"

            row = [f"<td>{esc(ad)}</td>"]
            for t in targets:
                row.append(f"<td>{'✅' if tgt_map.get(t, False) else '❌'}</td>")
            row.append(f"<td class='mono'>{esc(e5)}</td>")
            row.append(f"<td class='mono'>{esc(e6)}</td>")

            block.append("<tr>" + "".join(row) + "</tr>")
        block.append("</table>")
    else:
        block.append('<p><em>No availability domains captured from old instances (skipping per-AD catalog view).</em></p>')

    # Limits
    block.append('<h3>Tenant limits & availability</h3>')
    if service_name:
        block.append(f"<p><strong>Limits service name:</strong> <code>{service_name}</code></p>")

    limits_rows = advice.get("limits", []) or []
    if not limits_rows:
        block.append('<p><em>No limit data returned.</em></p>')
    else:
        block.append('<table class="advisor-table">')
        block.append(
            '<tr>'
            '<th>Limit</th>'
            '<th>GLOBAL limit</th>'
            '<th>REGION limit</th>'
            '<th>AD limits</th>'
            '<th>Available</th>'
            '<th>Used</th>'
            '<th>Effective quota</th>'
            '</tr>'
        )

        for item in limits_rows:
            name = esc(item.get("name", ""))
            g = item.get("global", None)
            r = item.get("region", None)
            ad_vals = item.get("ad_vals", {}) or {}
            ra = item.get("availability", {}) or {}

            ad_str = ", ".join([f"{k}={ad_vals[k]}" for k in ad_vals.keys()]) if ad_vals else ""

            block.append(
                "<tr>"
                f"<td class='mono'>{name}</td>"
                f"<td>{fmt(g)}</td>"
                f"<td>{fmt(r)}</td>"
                f"<td class='mono'>{esc(ad_str)}</td>"
                f"<td>{fmt(ra.get('available', None))}</td>"
                f"<td>{fmt(ra.get('used', None))}</td>"
                f"<td>{fmt(ra.get('effective_quota_value', None))}</td>"
                "</tr>"
            )

        block.append("</table>")

    block.append(
        "<p class='note'>"
        "Note: Shape catalog presence indicates the shape is offered in the region/AD. "
        "It does not guarantee real-time capacity. Limits/availability reflect tenancy quotas where available."
        "</p>"
    )

    block.append("</div>")
    return "\n".join(block)


def print_upgrade_advice_console(advice: Dict[str, Any]) -> None:
    """Optional console output for fast CLI visibility."""
    if not advice:
        return
    c = advice.get("counts", {}) or {}
    print()
    print("👑 KING KAI — Upgrade Advisor")
    print(f"   Old-shape pattern: {advice.get('pattern', '')}")
    print("--------------------------------------------------")
    print(f"Old instances found (explicit zeros):")
    print(f"  AMD:   E2={c.get('AMD_E2',0)} | E3={c.get('AMD_E3',0)} | E4={c.get('AMD_E4',0)}")
    print(f"  Intel: Standard1={c.get('INTEL_STD1',0)} | Standard2={c.get('INTEL_STD2',0)}")

    targets = advice.get("targets", []) or []
    if not targets:
        print("No relevant old families detected → no upgrade targets.")
        print("--------------------------------------------------")
        return

    if advice.get("amd_present"):
        print(f"  AMD upgrade targets: {', '.join(AMD_UPGRADE_TARGETS)}")
    if advice.get("intel_present"):
        print(f"  Intel upgrade targets: {', '.join(INTEL_UPGRADE_TARGETS)}")

    print("--------------------------------------------------")
    print()


# ------------------------------------------------------------
#  🔍 Scanner: Inspect a single compartment for forgotten resources
# ------------------------------------------------------------
def scan_compartment(
    comp_id: str,
    compute_client,
    blockstorage_client,
    network_client,
    lb_client,
    old_shape_pattern: str,
    suspicious_name_regex: str,
    availability_domains: List[str],
    old_gen_tracker: Optional[List[Dict[str, Any]]] = None,
):
    findings: List[Dict[str, Any]] = []

    # ---------------------------------------------
    # 0) Pre-fetch once for performance where possible
    # ---------------------------------------------
    # Volume attachments: one call per compartment (instead of per volume)
    attached_volume_ids = set()
    try:
        all_attachments = oci.pagination.list_call_get_all_results(
            compute_client.list_volume_attachments,
            compartment_id=comp_id
        ).data
        for att in all_attachments:
            # consider attached unless explicitly detached
            if getattr(att, "lifecycle_state", "").upper() != "DETACHED":
                vid = getattr(att, "volume_id", None)
                if vid:
                    attached_volume_ids.add(vid)
    except Exception:
        # If this fails, we fall back to per-volume attachment calls (not ideal, but robust)
        attached_volume_ids = None

    # 1) 🍞 Orphaned Block Volumes (no attachments → wasted storage cost)
    vols = oci.pagination.list_call_get_all_results(
        blockstorage_client.list_volumes,
        compartment_id=comp_id
    ).data

    for vol in vols:
        # Focus on volumes that are "AVAILABLE" (most likely orphan candidates)
        # Still okay if other states appear; keep it safe.
        vol_id = getattr(vol, "id", None)
        if not vol_id:
            continue

        is_orphan = False
        if attached_volume_ids is not None:
            is_orphan = (vol_id not in attached_volume_ids)
        else:
            # fallback: per-volume attachment check
            attachments = compute_client.list_volume_attachments(
                compartment_id=comp_id,
                volume_id=vol_id
            ).data
            is_orphan = (len(attachments) == 0)

        if is_orphan:
            size_gb = getattr(vol, "size_in_gbs", 0) or 0
            est_cost = round(size_gb * 0.025, 2)  # rough estimate: $0.025 per GB/mo
            findings.append({
                "ResourceName": getattr(vol, "display_name", vol_id),
                "ResourceType": "BlockVolume",
                "CompartmentId": comp_id,
                "Issue": "Orphaned Block Volume",
                "RiskLevel": "High",
                "CostEstimate": f"${est_cost}/mo" if size_gb > 0 else "Unknown",
                "AdditionalInfo": f"Size: {size_gb} GB, AD: {getattr(vol, 'availability_domain', '')}, OCID: {vol_id}",
                "FreeformTags": getattr(vol, "freeform_tags", {}) or {},
                "DefinedTags": getattr(vol, "defined_tags", {}) or {},
            })

    # 2) 🏷️ Unattached Reserved Public IPs (REGION + AD scope)
    public_ips: List[Any] = []

    # REGION scope
    try:
        region_pips = oci.pagination.list_call_get_all_results(
            network_client.list_public_ips,
            compartment_id=comp_id,
            scope="REGION"
        ).data
        public_ips.extend(region_pips)
    except Exception:
        pass

    # AD scope (needs availability_domain)
    for ad in availability_domains or []:
        try:
            ad_pips = oci.pagination.list_call_get_all_results(
                network_client.list_public_ips,
                compartment_id=comp_id,
                scope="AVAILABILITY_DOMAIN",
                availability_domain=ad
            ).data
            public_ips.extend(ad_pips)
        except Exception:
            continue

    for pip in public_ips:
        try:
            lifetime = getattr(pip, "lifetime", "")
            if str(lifetime).upper() != "RESERVED":
                continue

            # “Unattached” if no assignment fields
            private_ip_id = getattr(pip, "private_ip_id", None)
            assigned_entity_id = getattr(pip, "assigned_entity_id", None)
            assigned_entity_type = getattr(pip, "assigned_entity_type", None)

            if private_ip_id or assigned_entity_id or assigned_entity_type:
                continue

            # Cost estimate: rough reserved IP monthly cost (varies by region/pricing)
            est_cost = "$3.65/mo"

            ip_addr = getattr(pip, "ip_address", "")
            pip_id = getattr(pip, "id", "")
            findings.append({
                "ResourceName": getattr(pip, "display_name", None) or ip_addr or pip_id,
                "ResourceType": "PublicIP",
                "CompartmentId": comp_id,
                "Issue": "Unattached Reserved Public IP",
                "RiskLevel": "Medium",
                "CostEstimate": est_cost,
                "AdditionalInfo": f"IP: {ip_addr}, Scope: {getattr(pip, 'scope', '')}, OCID: {pip_id}",
                "FreeformTags": getattr(pip, "freeform_tags", {}) or {},
                "DefinedTags": getattr(pip, "defined_tags", {}) or {},
            })
        except Exception:
            continue

    # 3) 🔒 Empty NSGs (NSGs with zero VNICs)
    nsgs = oci.pagination.list_call_get_all_results(
        network_client.list_network_security_groups,
        compartment_id=comp_id
    ).data

    for nsg in nsgs:
        try:
            attached_vnics = network_client.list_network_security_group_vnics(
                network_security_group_id=nsg.id
            ).data
            if len(attached_vnics) == 0:
                findings.append({
                    "ResourceName": getattr(nsg, "display_name", nsg.id),
                    "ResourceType": "NetworkSecurityGroup",
                    "CompartmentId": comp_id,
                    "Issue": "Empty NSG",
                    "RiskLevel": "Low",
                    "CostEstimate": "Free",
                    "AdditionalInfo": f"No attached VNICs, OCID: {nsg.id}",
                    "FreeformTags": getattr(nsg, "freeform_tags", {}) or {},
                    "DefinedTags": getattr(nsg, "defined_tags", {}) or {},
                })
        except Exception:
            continue

    # 4) ⚖️ Load Balancers with no backends (validated via list_backends)
    lbs = oci.pagination.list_call_get_all_results(
        lb_client.list_load_balancers,
        compartment_id=comp_id
    ).data

    for lb in lbs:
        try:
            lb_id = lb.id
            details = lb_client.get_load_balancer(load_balancer_id=lb_id).data

            # Determine if ANY backend exists across ANY backend set
            has_any_backend = False
            try:
                backend_sets = oci.pagination.list_call_get_all_results(
                    lb_client.list_backend_sets,
                    load_balancer_id=lb_id
                ).data

                for bs in backend_sets:
                    bs_name = getattr(bs, "name", None)
                    if not bs_name:
                        continue
                    backends = oci.pagination.list_call_get_all_results(
                        lb_client.list_backends,
                        load_balancer_id=lb_id,
                        backend_set_name=bs_name
                    ).data
                    if backends and len(backends) > 0:
                        has_any_backend = True
                        break
            except Exception:
                # If we can't validate, skip (avoid false positives)
                continue

            if not has_any_backend:
                findings.append({
                    "ResourceName": getattr(lb, "display_name", lb_id),
                    "ResourceType": "LoadBalancer",
                    "CompartmentId": comp_id,
                    "Issue": "Load Balancer with No Backends",
                    "RiskLevel": "High",
                    "CostEstimate": "$18.25/mo",  # rough estimate
                    "AdditionalInfo": f"Shape: {getattr(details,'shape_name','')}, SubnetCount: {len(getattr(details,'subnet_ids',[]) or [])}, OCID: {lb_id}",
                    "FreeformTags": getattr(lb, "freeform_tags", {}) or {},
                    "DefinedTags": getattr(lb, "defined_tags", {}) or {},
                })
        except oci.exceptions.ServiceError:
            continue
        except Exception:
            continue

    # 5) 👴 Old-Generation Instances (shape matches regex)
    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=comp_id
    ).data

    for inst in instances:
        shape = getattr(inst, "shape", "") or ""
        if re.match(old_shape_pattern, shape):
            if old_gen_tracker is not None:
                old_gen_tracker.append({
                    "name": getattr(inst, "display_name", inst.id),
                    "shape": shape,
                    "availability_domain": getattr(inst, "availability_domain", None),
                    "compartment_id": comp_id,
                    "lifecycle_state": getattr(inst, "lifecycle_state", None),
                    "ocid": getattr(inst, "id", None),
                })

            findings.append({
                "ResourceName": getattr(inst, "display_name", inst.id),
                "ResourceType": "ComputeInstance",
                "CompartmentId": comp_id,
                "Issue": f"Old-gen Shape ({shape})",
                "RiskLevel": "Medium",
                "CostEstimate": "Varies",
                "AdditionalInfo": f"Shape: {shape}, Lifecycle: {getattr(inst,'lifecycle_state','')}, OCID: {getattr(inst,'id','')}",
                "FreeformTags": getattr(inst, "freeform_tags", {}) or {},
                "DefinedTags": getattr(inst, "defined_tags", {}) or {},
            })

    # 6) 🏷️ Resources with ZERO TAGS
    resource_collections = [
        ("ComputeInstance", instances),
        ("BlockVolume", vols),
        ("PublicIP", public_ips),
        ("NetworkSecurityGroup", nsgs),
        ("LoadBalancer", lbs),
    ]
    for rtype, coll in resource_collections:
        for res in coll:
            try:
                if has_no_tags(res):
                    name = getattr(res, "display_name", None) or getattr(res, "ip_address", None) or getattr(res, "id", "<unknown>")
                    rid = getattr(res, "id", "")
                    findings.append({
                        "ResourceName": name,
                        "ResourceType": rtype,
                        "CompartmentId": comp_id,
                        "Issue": "No Tags",
                        "RiskLevel": "High",
                        "CostEstimate": "Varies",
                        "AdditionalInfo": f"OCID: {rid}" if rid else "",
                        "FreeformTags": getattr(res, "freeform_tags", {}) or {},
                        "DefinedTags": getattr(res, "defined_tags", {}) or {},
                    })
            except Exception:
                continue

    # 7) 🚩 Suspicious Name Patterns
    for rtype, coll in resource_collections:
        for res in coll:
            try:
                name = getattr(res, "display_name", None) or str(getattr(res, "ip_address", "")) or getattr(res, "id", "<unknown>")
                if re.search(suspicious_name_regex, str(name), re.IGNORECASE):
                    rid = getattr(res, "id", "")
                    findings.append({
                        "ResourceName": name,
                        "ResourceType": rtype,
                        "CompartmentId": comp_id,
                        "Issue": "Suspicious Name Pattern",
                        "RiskLevel": "High",
                        "CostEstimate": "Varies",
                        "AdditionalInfo": f"OCID: {rid}" if rid else "",
                        "FreeformTags": getattr(res, "freeform_tags", {}) or {},
                        "DefinedTags": getattr(res, "defined_tags", {}) or {},
                    })
            except Exception:
                continue

    return findings


# ------------------------------------------------------------
#  🔥 Main Entrypoint
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — OCI Forgotten Resource Detective (Python edition)")

    parser.add_argument(
        "--profile",
        required=False,
        default="DEFAULT",
        help="OCI CLI profile name (default: DEFAULT) from ~/.oci/config"
    )
    parser.add_argument(
        "--old-shape-pattern",
        required=False,
        default=r"VM\.Standard1.*",
        help=r"Regex to flag old-gen compute shapes (default: VM\.Standard1.*)"
    )
    parser.add_argument(
        "--output-csv",
        required=False,
        default="forgotten_resources_report.csv",
        help="Path to write CSV report (default: forgotten_resources_report.csv)"
    )
    parser.add_argument(
        "--output-html",
        required=False,
        default="forgotten_resources_report.html",
        help="Path to write HTML report (default: forgotten_resources_report.html)"
    )
    parser.add_argument(
        "--suspicious-name-regex",
        required=False,
        default=r"\b(test|temp|demo|old|backup|poc)\b",
        help=r"Regex for sketchy resource names (default: \b(test|temp|demo|old|backup|poc)\b)"
    )

    args = parser.parse_args()

    # Load OCI config and clients
    try:
        config = oci.config.from_file(profile_name=args.profile)
    except Exception as e:
        print(f"❌ Failed to load OCI config for profile '{args.profile}': {e}")
        sys.exit(1)

    tenancy_id = config.get("tenancy")
    if not tenancy_id:
        print("❌ Couldn’t find 'tenancy' in OCI config. Exiting.")
        sys.exit(1)

    identity_client     = oci.identity.IdentityClient(config)
    compute_client      = oci.core.ComputeClient(config)
    blockstorage_client = oci.core.BlockstorageClient(config)
    network_client      = oci.core.VirtualNetworkClient(config)
    lb_client           = oci.load_balancer.LoadBalancerClient(config)
    limits_client       = oci.limits.LimitsClient(config)

    # Availability domains list (needed for AD-scoped public IPs)
    availability_domains: List[str] = []
    try:
        ads = oci.pagination.list_call_get_all_results(
            identity_client.list_availability_domains,
            compartment_id=tenancy_id
        ).data
        availability_domains = [getattr(ad, "name", "") for ad in ads if getattr(ad, "name", "")]
    except Exception:
        availability_domains = []

    print(f"🔍 Fetching all active compartments under tenancy {tenancy_id} …")
    compartments, comp_name_by_id = collect_all_compartments(identity_client, tenancy_id)
    print(f"   Found {len(compartments)} compartments (including root).")
    print()

    all_findings: List[Dict[str, Any]] = []
    old_gen_tracker: List[Dict[str, Any]] = []

    # Scan each compartment
    for comp_id in compartments:
        print(f"⏳ Scanning compartment {comp_id} …")
        compartment_findings = scan_compartment(
            comp_id=comp_id,
            compute_client=compute_client,
            blockstorage_client=blockstorage_client,
            network_client=network_client,
            lb_client=lb_client,
            old_shape_pattern=args.old_shape_pattern,
            suspicious_name_regex=args.suspicious_name_regex,
            availability_domains=availability_domains,
            old_gen_tracker=old_gen_tracker,
        )
        all_findings.extend(compartment_findings)

    # Compute KING KAI upgrade advice (for console + HTML)
    upgrade_advice = compute_upgrade_advice(
        old_gen_tracker=old_gen_tracker,
        compute_client=compute_client,
        limits_client=limits_client,
        tenancy_id=tenancy_id,
        old_shape_pattern=args.old_shape_pattern
    )

    # Console: always print advisor (explicit zeros are important)
    print_upgrade_advice_console(upgrade_advice)

    # Report summary
    if not all_findings:
        print("✅ All clean! No forgotten clouds here.")
    else:
        print(f"⚠️  Found {len(all_findings)} forgotten/suspicious findings in total.")

    # --------------- Generate CSV Report ----------------
    csv_path = args.output_csv
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "ResourceName", "ResourceType", "CompartmentId",
            "Issue", "RiskLevel", "CostEstimate",
            "AdditionalInfo", "FreeformTags", "DefinedTags"
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for item in all_findings:
            # Do not mutate original item (keep HTML nicer)
            row = dict(item)
            row["FreeformTags"] = "{}" if not row.get("FreeformTags") else str(row["FreeformTags"])
            row["DefinedTags"] = "{}" if not row.get("DefinedTags") else str(row["DefinedTags"])
            writer.writerow(row)

    print(f"🗒️  CSV report saved to: {csv_path}")

    # --------------- Generate HTML Report ----------------
    html_path = args.output_html
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    advisor_html = render_upgrade_advice_html(upgrade_advice)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>OCI Forgotten Resource Detective Report</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f9f9f9; margin: 20px; }}
        h1 {{ color: #2c3e50; font-size: 28px; margin-bottom: 6px; }}
        h2 {{ margin-top: 0; }}
        p {{ font-size: 14px; }}

        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ padding: 10px; border: 1px solid #ddd; text-align: left; font-size: 13px; vertical-align: top; }}
        th {{ background: #34495e; color: white; }}
        tr:nth-child(even) {{ background: #f2f2f2; }}

        .high {{ background: #fdecea; }}   /* Light red for high risk */
        .medium {{ background: #fff4e5; }} /* Light orange for medium */
        .low {{ background: #e8f5e9; }}    /* Light green for low */

        .footer {{ margin-top: 30px; font-size: 12px; color: #555; }}

        /* 👑 KING KAI advisor styles */
        .kingsai {{ background: #ffffff; border: 1px solid #e3e3e3; padding: 16px; border-radius: 10px; margin-top: 16px; }}
        .kpi-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 10px 0 0 0; }}
        .kpi {{ background:#fafafa; border:1px solid #eee; border-radius:10px; padding:10px 12px; min-width: 240px; }}
        .kpi-title {{ font-size: 12px; color:#666; margin-bottom: 4px; }}
        .kpi-val {{ font-size: 14px; font-weight: 600; color:#222; }}
        .advisor-table {{ width:100%; border-collapse: collapse; margin-top:10px; }}
        .advisor-table th, .advisor-table td {{ padding: 8px; border: 1px solid #ddd; font-size: 13px; }}
        .advisor-table th {{ background: #2c3e50; color: white; }}
        .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 12px; }}
        .note {{ font-size: 12px; color:#555; margin-top: 10px; }}
    </style>
</head>
<body>
    <h1>🕵️ OCI Forgotten Resource Detective</h1>
    <p><strong>Generated:</strong> {esc(now)}</p>
    <p><strong>Total Issues Found:</strong> {len(all_findings)}</p>

    {advisor_html}

    <table>
        <tr>
            <th>Risk</th>
            <th>Resource Name</th>
            <th>Type</th>
            <th>Compartment</th>
            <th>Issue</th>
            <th>Cost Estimate</th>
            <th>Additional Info</th>
            <th>Tags</th>
        </tr>
"""

    for item in all_findings:
        risk = (item.get("RiskLevel", "") or "").lower().strip() or "low"

        comp_id = item.get("CompartmentId", "")
        comp_name = comp_name_by_id.get(comp_id, "")
        comp_cell = f"{esc(comp_name)}<br/><span class='mono'>{esc(comp_id)}</span>" if comp_name else f"<span class='mono'>{esc(comp_id)}</span>"

        tags_str = ""
        ff = item.get("FreeformTags") or {}
        dt = item.get("DefinedTags") or {}
        if ff:
            tags_str += f"FF: {esc(ff)}<br/>"
        if dt:
            tags_str += f"DT: {esc(dt)}"

        html_content += f"""
        <tr class="{esc(risk)}">
            <td>{esc(item.get('RiskLevel',''))}</td>
            <td>{esc(item.get('ResourceName',''))}</td>
            <td>{esc(item.get('ResourceType',''))}</td>
            <td>{comp_cell}</td>
            <td>{esc(item.get('Issue',''))}</td>
            <td>{esc(item.get('CostEstimate',''))}</td>
            <td>{esc(item.get('AdditionalInfo',''))}</td>
            <td>{tags_str}</td>
        </tr>
"""

    html_content += """
    </table>

    <div class="footer">
        <p>🔍 Report generated by OCI Forgotten Resource Detective (Python, CloudCostChefs + KING KAI edition)</p>
        <p>⚠️ Review each finding before deleting! Verify dependencies and confirm with app owners.</p>
    </div>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️  HTML report saved to: {html_path}")


if __name__ == "__main__":
    main()
