#!/usr/bin/env python3
"""
👑 KING KAI — OCI Shapes Upgrade Report

Shapes-only mode (recommended):
  python3 oci_forgotten_resources_king_kai.py --shapes-upgrade-report

What it does:
- Scans ALL compartments (root + active sub-compartments)
- Finds instances that match the predefined "old shapes" list (AMD E2/E3/E4 + Intel Standard2 sizes)
- Produces HTML + CSV with:
    * per-instance risk level
    * per-instance upgrade recommendations
    * ✅/❌ for upgrade targets based on:
        - shape offered in region/AD catalog (list_shapes)
        - tenancy/quota availability signals (Limits resource availability where supported)
"""

import oci
import argparse
import csv
import re
import sys
import html as htmlmod
from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set, Tuple


# ------------------------------------------------------------
#  Old shapes to scan (as requested)
# ------------------------------------------------------------
OLD_SHAPES_SET: Set[str] = {
    # AMD E2 (explicit sizes)
    "VM.Standard.E2.1",
    "VM.Standard.E2.2",
    "VM.Standard.E2.4",
    "VM.Standard.E2.8",
    # AMD E3/E4 flex
    "VM.Standard.E3.Flex",
    "VM.Standard.E4.Flex",
    # Intel Standard2 (explicit sizes)
    "VM.Standard2.1",
    "VM.Standard2.2",
    "VM.Standard2.4",
    "VM.Standard2.8",
    "VM.Standard2.16",
    "VM.Standard2.24",
}

# Upgrade targets
AMD_UPGRADE_TARGETS = ["VM.Standard.E5.Flex", "VM.Standard.E6.Flex"]
INTEL_UPGRADE_TARGETS = ["VM.Standard3.Flex", "VM.Optimized3.Flex"]


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def esc(s: Any) -> str:
    if s is None:
        return ""
    return htmlmod.escape(str(s), quote=True)


def collect_all_compartments(identity_client, tenancy_id: str):
    """
    Returns:
      - compartment_ids: [tenancy_id, <all active sub-compartment OCIDs>]
      - comp_name_by_id: { ocid: name }
    """
    comp_ids: List[str] = []
    comp_name_by_id: Dict[str, str] = {}

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


def list_availability_domains(identity_client, tenancy_id: str) -> List[str]:
    try:
        ads = oci.pagination.list_call_get_all_results(
            identity_client.list_availability_domains,
            compartment_id=tenancy_id
        ).data
        return [getattr(ad, "name", "") for ad in ads if getattr(ad, "name", "")]
    except Exception:
        return []


# ------------------------------------------------------------
#  Risk and upgrade mapping (per instance)
# ------------------------------------------------------------
def risk_for_shape(shape: str) -> str:
    """
    HIGH:
      - AMD VM.Standard.E2.*
      - AMD VM.Standard.E3.Flex
    MEDIUM:
      - everything else in old list
    """
    if re.match(r"^VM\.Standard\.E2\.\d+$", shape):
        return "High"
    if shape == "VM.Standard.E3.Flex":
        return "High"
    return "Medium"


def upgrade_targets_for_shape(shape: str) -> List[str]:
    """
    AMD:
      E2.* / E3.Flex / E4.Flex -> E5/E6 Flex
    Intel:
      Standard2.* -> Standard3.Flex / Optimized3.Flex
    """
    if re.match(r"^VM\.Standard\.E2\.\d+$", shape) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex"):
        return AMD_UPGRADE_TARGETS
    if re.match(r"^VM\.Standard2\.\d+$", shape):
        return INTEL_UPGRADE_TARGETS
    return []


# ------------------------------------------------------------
#  Limits helpers (to mirror Console "Limits, quotas and usage")
# ------------------------------------------------------------
def discover_compute_service_name(limits_client, compartment_id: str) -> str:
    """
    Limits service uses a service_name string (often 'compute').
    We attempt to discover it, defaulting to 'compute'.
    """
    try:
        svcs = oci.pagination.list_call_get_all_results(
            limits_client.list_services,
            compartment_id=compartment_id
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


def build_limit_value_index(limits_client, compartment_id: str, service_name: str) -> Dict[Tuple[str, str, Optional[str]], Any]:
    """
    index[(limit_name, scope_type, availability_domain)] = value
    """
    index: Dict[Tuple[str, str, Optional[str]], Any] = {}
    try:
        vals = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=compartment_id,
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
    compartment_id: str,
    limit_name: str,
    availability_domain: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    Try to fetch used/available/effective_quota_value (region first, then AD).
    """
    try:
        ra = limits_client.get_resource_availability(
            service_name=service_name,
            limit_name=limit_name,
            compartment_id=compartment_id
        ).data
        return {
            "available": getattr(ra, "available", None),
            "used": getattr(ra, "used", None),
            "effective_quota_value": getattr(ra, "effective_quota_value", None),
        }
    except oci.exceptions.ServiceError:
        if availability_domain:
            try:
                ra = limits_client.get_resource_availability(
                    service_name=service_name,
                    limit_name=limit_name,
                    compartment_id=compartment_id,
                    availability_domain=availability_domain
                ).data
                return {
                    "available": getattr(ra, "available", None),
                    "used": getattr(ra, "used", None),
                    "effective_quota_value": getattr(ra, "effective_quota_value", None),
                }
            except oci.exceptions.ServiceError:
                return None
        return None


def find_limit_names_for_target(all_limit_names: Set[str], target_shape: str) -> List[str]:
    """
    Dynamically find the correct limit names (often '*-regional-count') for each target.
    We pick 1 core + 1 memory limit where possible.
    """
    if target_shape == "VM.Standard.E5.Flex":
        prefixes = ["standard-e5"]
    elif target_shape == "VM.Standard.E6.Flex":
        prefixes = ["standard-e6"]
    elif target_shape == "VM.Standard3.Flex":
        prefixes = ["standard3"]
    elif target_shape == "VM.Optimized3.Flex":
        prefixes = ["optimized3"]
    else:
        prefixes = []

    hits: List[str] = []
    for p in prefixes:
        core = sorted([n for n in all_limit_names if n.startswith(p) and "core" in n and ("regional" in n or n.endswith("count"))])
        mem  = sorted([n for n in all_limit_names if n.startswith(p) and "memory" in n and ("regional" in n or n.endswith("count"))])

        def prefer(names: List[str]) -> List[str]:
            return sorted(names, key=lambda x: (0 if "regional-count" in x else 1, len(x)))

        core = prefer(core)
        mem  = prefer(mem)

        if core:
            hits.append(core[0])
        if mem:
            hits.append(mem[0])

    return hits


# ------------------------------------------------------------
#  Shape catalog helpers
# ------------------------------------------------------------
def list_shapes_in_ad(compute_client, tenancy_id: str, ad: str) -> Set[str]:
    """
    AD shape catalog (offered shapes, not real-time capacity guarantee)
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


# ------------------------------------------------------------
#  Shapes-only scan
# ------------------------------------------------------------
def scan_compartment_shapes_only(
    comp_id: str,
    compute_client,
    old_shapes_set: Optional[Set[str]],
    old_shape_regex: Optional[re.Pattern],
    old_gen_tracker: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=comp_id
    ).data

    for inst in instances:
        shape = getattr(inst, "shape", "") or ""
        match = False
        if old_shapes_set is not None:
            match = shape in old_shapes_set
        elif old_shape_regex is not None:
            match = bool(old_shape_regex.match(shape))

        if not match:
            continue

        # Try to capture AD (some SDK summaries may miss it; fallback to get_instance)
        ad = getattr(inst, "availability_domain", None)
        if not ad:
            try:
                full = compute_client.get_instance(inst.id).data
                ad = getattr(full, "availability_domain", None)
            except Exception:
                ad = None

        lifecycle = getattr(inst, "lifecycle_state", None)
        name = getattr(inst, "display_name", inst.id)
        ocid = getattr(inst, "id", None)

        old_gen_tracker.append({
            "name": name,
            "shape": shape,
            "availability_domain": ad,
            "compartment_id": comp_id,
            "lifecycle_state": lifecycle,
            "ocid": ocid,
        })

        # Findings row (used for CSV mainly)
        findings.append({
            "ResourceName": name,
            "ResourceType": "ComputeInstance",
            "CompartmentId": comp_id,
            "Issue": f"Old-gen Shape ({shape})",
            "RiskLevel": risk_for_shape(shape),
            "CostEstimate": "Varies",
            "AdditionalInfo": f"Shape: {shape}, Lifecycle: {lifecycle}, OCID: {ocid}",
            "FreeformTags": getattr(inst, "freeform_tags", {}) or {},
            "DefinedTags": getattr(inst, "defined_tags", {}) or {},
        })

    return findings


# ------------------------------------------------------------
#  Upgrade advisor (global section + per-instance evaluation)
# ------------------------------------------------------------
def compute_upgrade_advice_summary(
    old_gen_tracker: List[Dict[str, Any]],
    compute_client,
    limits_client,
    tenancy_id: str,
    availability_domains: List[str],
) -> Dict[str, Any]:
    """
    Summary block for HTML header section (counts + catalog + limits list)
    """
    counts = Counter()
    ads_seen: Set[str] = set()

    for row in old_gen_tracker:
        shape = row.get("shape", "") or ""
        ad = row.get("availability_domain")
        if ad:
            ads_seen.add(ad)

        if re.match(r"^VM\.Standard\.E2\b", shape):
            counts["AMD_E2"] += 1
        elif re.match(r"^VM\.Standard\.E3\b", shape):
            counts["AMD_E3"] += 1
        elif re.match(r"^VM\.Standard\.E4\b", shape):
            counts["AMD_E4"] += 1
        elif re.match(r"^VM\.Standard2\b", shape):
            counts["INTEL_STD2"] += 1
        else:
            counts["OTHER"] += 1

    for k in ["AMD_E2", "AMD_E3", "AMD_E4", "INTEL_STD2"]:
        counts.setdefault(k, 0)

    amd_present = (counts["AMD_E2"] + counts["AMD_E3"] + counts["AMD_E4"]) > 0
    intel_present = counts["INTEL_STD2"] > 0

    targets: List[str] = []
    if amd_present:
        targets.extend(AMD_UPGRADE_TARGETS)
    if intel_present:
        targets.extend(INTEL_UPGRADE_TARGETS)

    advice: Dict[str, Any] = {
        "counts": dict(counts),
        "targets": targets,
        "ads": sorted(list(ads_seen)) if ads_seen else sorted([a for a in availability_domains if a]),
        "catalog": {},
        "limits": [],
        "compute_service": "",
        "target_to_limits": {},  # target -> [core_limit, mem_limit]
    }

    if not targets:
        return advice

    # Catalog per AD
    for ad in advice["ads"]:
        shape_set = list_shapes_in_ad(compute_client, tenancy_id, ad)
        advice["catalog"][ad] = {
            "targets": {t: (t in shape_set) for t in targets},
            "e5_series": sorted([s for s in shape_set if s.startswith("VM.Standard.E5")]),
            "e6_series": sorted([s for s in shape_set if s.startswith("VM.Standard.E6")]),
        }

    # Limits discovery: use tenancy/root like console default
    service_name = discover_compute_service_name(limits_client, tenancy_id)
    advice["compute_service"] = service_name

    limit_index = build_limit_value_index(limits_client, tenancy_id, service_name)
    all_limit_names = {k[0] for k in limit_index.keys() if k and k[0]}

    target_to_limits: Dict[str, List[str]] = {}
    for t in targets:
        target_to_limits[t] = find_limit_names_for_target(all_limit_names, t)

    advice["target_to_limits"] = target_to_limits

    # Build a compact limits table (service limit + usage + available)
    needed_limits: Set[str] = set()
    for t, lims in target_to_limits.items():
        for ln in lims:
            needed_limits.add(ln)

    needed_limits_sorted = sorted(needed_limits)
    any_ad = advice["ads"][0] if advice["ads"] else None

    for limit_name in needed_limits_sorted:
        global_val = None
        region_val = None
        ad_vals: Dict[str, Any] = {}

        for (lname, scope, ad), val in limit_index.items():
            if lname != limit_name:
                continue
            if scope == "GLOBAL":
                global_val = val
            elif scope == "REGION":
                region_val = val
            elif scope == "AD":
                ad_vals[str(ad)] = val

        ra = get_resource_availability_safe(
            limits_client=limits_client,
            service_name=service_name,
            compartment_id=tenancy_id,
            limit_name=limit_name,
            availability_domain=any_ad
        ) or {}

        advice["limits"].append({
            "name": limit_name,
            "global": global_val,
            "region": region_val,
            "ad_vals": {k: ad_vals[k] for k in sorted(ad_vals.keys())} if ad_vals else {},
            "availability": ra,
        })

    return advice


def evaluate_upgrade_option(
    target_shape: str,
    instance_ad: Optional[str],
    instance_compartment_id: str,
    shapes_cache_by_ad: Dict[str, Set[str]],
    target_to_limits: Dict[str, List[str]],
    limits_client,
    service_name: str,
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]],
) -> Tuple[bool, str]:
    """
    Returns (is_ok, reason_string_for_html)

    ✅ requires:
      - target shape offered in AD catalog
      - AND (if availability/quota info exists) not zero

    If AD is unknown -> ❌ (unknown AD)
    """
    if not instance_ad:
        return (False, "unknown AD")

    ad_shapes = shapes_cache_by_ad.get(instance_ad, set())
    if target_shape not in ad_shapes:
        return (False, "not offered")

    # If we don't know limit names for this target, we can only assert catalog presence
    limit_names = target_to_limits.get(target_shape, [])
    if not limit_names:
        return (True, "catalog ok")

    # Evaluate quota/available signals at the INSTANCE compartment level (reflects quota policy)
    for ln in limit_names:
        key = (instance_compartment_id, ln, instance_ad)
        if key not in ra_cache:
            ra_cache[key] = get_resource_availability_safe(
                limits_client=limits_client,
                service_name=service_name,
                compartment_id=instance_compartment_id,
                limit_name=ln,
                availability_domain=instance_ad
            )
        ra = ra_cache[key]

        # If supported, use explicit available/effective quota
        if ra:
            eff = ra.get("effective_quota_value", None)
            avail = ra.get("available", None)

            # If any relevant quota is 0 -> treat as ❌ (user explicitly wants to see 0s as blocking)
            if eff == 0:
                return (False, f"{ln} quota=0")
            if avail == 0:
                return (False, f"{ln} available=0")

    return (True, "ok")


# ------------------------------------------------------------
#  HTML rendering
# ------------------------------------------------------------
def render_advisor_block_html(advice: Dict[str, Any]) -> str:
    if not advice:
        return ""

    c = advice.get("counts", {}) or {}
    amd_line = f"E2={c.get('AMD_E2',0)} | E3={c.get('AMD_E3',0)} | E4={c.get('AMD_E4',0)}"
    intel_line = f"Standard2={c.get('INTEL_STD2',0)}"
    targets: List[str] = advice.get("targets", []) or []
    ads: List[str] = advice.get("ads", []) or []
    service_name = esc(advice.get("compute_service", ""))

    def fmt(v):
        return "" if v is None else esc(v)  # keep 0 visible

    out: List[str] = []
    out.append('<div class="kingsai">')
    out.append('<h2>👑 KING KAI — Upgrade Advisor</h2>')

    out.append('<div class="kpi-row">')
    out.append(f'<div class="kpi"><div class="kpi-title">AMD old instances</div><div class="kpi-val">{esc(amd_line)}</div></div>')
    out.append(f'<div class="kpi"><div class="kpi-title">Intel old instances</div><div class="kpi-val">{esc(intel_line)}</div></div>')
    out.append('</div>')

    if not targets:
        out.append('<p><em>No old shapes detected from the requested sets.</em></p>')
        out.append('</div>')
        return "\n".join(out)

    out.append('<h3>Recommended upgrade targets</h3>')
    out.append('<ul>')
    if (c.get("AMD_E2", 0) + c.get("AMD_E3", 0) + c.get("AMD_E4", 0)) > 0:
        out.append(f"<li><strong>AMD →</strong> {', '.join(map(esc, AMD_UPGRADE_TARGETS))}</li>")
    if c.get("INTEL_STD2", 0) > 0:
        out.append(f"<li><strong>Intel →</strong> {', '.join(map(esc, INTEL_UPGRADE_TARGETS))}</li>")
    out.append('</ul>')

    if ads:
        out.append('<h3>Shape catalog availability (per AD)</h3>')
        out.append('<table class="advisor-table">')
        out.append('<tr><th>Availability Domain</th>' + "".join([f"<th>{esc(t)}</th>" for t in targets]) +
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
            out.append("<tr>" + "".join(row) + "</tr>")
        out.append("</table>")

    out.append('<h3>Tenant limits, usage & available (console-style)</h3>')
    if service_name:
        out.append(f"<p><strong>Service:</strong> <code>{service_name}</code></p>")

    limits_rows = advice.get("limits", []) or []
    if not limits_rows:
        out.append('<p><em>No matching limits found for the target shapes (check naming in your tenancy).</em></p>')
    else:
        out.append('<table class="advisor-table">')
        out.append(
            '<tr>'
            '<th>Limit Name</th>'
            '<th>Service Limit (GLOBAL)</th>'
            '<th>Service Limit (REGION)</th>'
            '<th>Service Limit (AD)</th>'
            '<th>Usage</th>'
            '<th>Available</th>'
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

            out.append(
                "<tr>"
                f"<td class='mono'>{name}</td>"
                f"<td>{fmt(g)}</td>"
                f"<td>{fmt(r)}</td>"
                f"<td class='mono'>{esc(ad_str)}</td>"
                f"<td>{fmt(ra.get('used', None))}</td>"
                f"<td>{fmt(ra.get('available', None))}</td>"
                f"<td>{fmt(ra.get('effective_quota_value', None))}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append(
        "<p class='note'>"
        "✅/❌ at instance level uses AD catalog + quota/availability signals (when supported). "
        "Catalog presence does not guarantee real-time capacity."
        "</p>"
    )
    out.append("</div>")
    return "\n".join(out)


# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — Shapes Upgrade Report")

    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name from ~/.oci/config (default: DEFAULT)")
    parser.add_argument("--output-csv", default="forgotten_resources_report.csv", help="CSV path (default: forgotten_resources_report.csv)")
    parser.add_argument("--output-html", default="forgotten_resources_report.html", help="HTML path (default: forgotten_resources_report.html)")

    parser.add_argument("--shapes-upgrade-report", action="store_true",
                        help="Scan predefined old shapes (AMD E2/E3/E4 + Intel Standard2) and produce upgrade advisor.")
    parser.add_argument("--old-shape-pattern", default=None,
                        help="Optional regex to define 'old' shapes. If provided, overrides the predefined set.")

    args = parser.parse_args()

    if not args.shapes_upgrade_report and not args.old_shape_pattern:
        print("❌ Please run with --shapes-upgrade-report (recommended) or --old-shape-pattern <regex>.")
        sys.exit(2)

    # Load OCI config
    try:
        config = oci.config.from_file(profile_name=args.profile)
    except Exception as e:
        print(f"❌ Failed to load OCI config for profile '{args.profile}': {e}")
        sys.exit(1)

    tenancy_id = config.get("tenancy")
    if not tenancy_id:
        print("❌ Couldn’t find 'tenancy' in OCI config. Exiting.")
        sys.exit(1)

    identity_client = oci.identity.IdentityClient(config)
    compute_client  = oci.core.ComputeClient(config)
    limits_client   = oci.limits.LimitsClient(config)

    compartments, comp_name_by_id = collect_all_compartments(identity_client, tenancy_id)
    availability_domains = list_availability_domains(identity_client, tenancy_id)

    # Shapes match configuration
    old_shapes_set: Optional[Set[str]] = None
    old_shape_regex: Optional[re.Pattern] = None

    if args.old_shape_pattern:
        try:
            old_shape_regex = re.compile(args.old_shape_pattern)
        except re.error as e:
            print(f"❌ Invalid regex for --old-shape-pattern: {e}")
            sys.exit(1)
    else:
        old_shapes_set = set(OLD_SHAPES_SET)

    print(f"🔍 Scanning tenancy {tenancy_id} (shapes-only)…")
    print(f"   Compartments: {len(compartments)} (including root)")
    print()

    all_findings: List[Dict[str, Any]] = []
    old_gen_tracker: List[Dict[str, Any]] = []

    for comp_id in compartments:
        comp_findings = scan_compartment_shapes_only(
            comp_id=comp_id,
            compute_client=compute_client,
            old_shapes_set=old_shapes_set,
            old_shape_regex=old_shape_regex,
            old_gen_tracker=old_gen_tracker,
        )
        all_findings.extend(comp_findings)

    # Summary advisor (counts + catalog + tenant limits table)
    advice = compute_upgrade_advice_summary(
        old_gen_tracker=old_gen_tracker,
        compute_client=compute_client,
        limits_client=limits_client,
        tenancy_id=tenancy_id,
        availability_domains=availability_domains,
    )

    # Build caches for per-instance ✅/❌ evaluation
    shapes_cache_by_ad: Dict[str, Set[str]] = {}
    for ad in (advice.get("ads", []) or availability_domains):
        if ad and ad not in shapes_cache_by_ad:
            shapes_cache_by_ad[ad] = list_shapes_in_ad(compute_client, tenancy_id, ad)

    service_name = advice.get("compute_service") or discover_compute_service_name(limits_client, tenancy_id)
    target_to_limits: Dict[str, List[str]] = advice.get("target_to_limits", {}) or {}

    # Cache resource availability calls: (compartment_id, limit_name, ad) -> ra
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]] = {}

    # --------------- Generate CSV ----------------
    csv_path = args.output_csv
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "Risk", "Shape", "InstanceName", "Lifecycle", "CompartmentId", "OCID", "UpgradeOptions"
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for row in old_gen_tracker:
            shape = row.get("shape", "")
            risk = risk_for_shape(shape)
            targets = upgrade_targets_for_shape(shape)

            upgrades_str_parts = []
            for t in targets:
                ok, reason = evaluate_upgrade_option(
                    target_shape=t,
                    instance_ad=row.get("availability_domain"),
                    instance_compartment_id=row.get("compartment_id", ""),
                    shapes_cache_by_ad=shapes_cache_by_ad,
                    target_to_limits=target_to_limits,
                    limits_client=limits_client,
                    service_name=service_name,
                    ra_cache=ra_cache,
                )
                upgrades_str_parts.append(f"{t} [{'OK' if ok else 'NO'}:{reason}]")

            writer.writerow({
                "Risk": risk,
                "Shape": shape,
                "InstanceName": row.get("name", ""),
                "Lifecycle": row.get("lifecycle_state", ""),
                "CompartmentId": row.get("compartment_id", ""),
                "OCID": row.get("ocid", ""),
                "UpgradeOptions": " | ".join(upgrades_str_parts),
            })

    print(f"🗒️  CSV report saved to: {csv_path}")

    # --------------- Generate HTML ----------------
    html_path = args.output_html
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    advisor_html = render_advisor_block_html(advice)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>KING KAI — Shapes Upgrade Report</title>
  <style>
    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f9f9f9; margin: 20px; }}
    h1 {{ color:#2c3e50; font-size:28px; margin-bottom: 6px; }}
    p {{ font-size:14px; }}

    table {{ width:100%; border-collapse:collapse; margin-top:16px; }}
    th, td {{ padding:10px; border:1px solid #ddd; text-align:left; font-size:13px; vertical-align: top; }}
    th {{ background:#34495e; color:white; }}
    tr:nth-child(even) {{ background:#f2f2f2; }}

    .highrow {{ background: #fdecea !important; }}   /* light red */
    .medrow  {{ background: #fff4e5 !important; }}   /* light orange */

    /* Advisor */
    .kingsai {{ background:#fff; border:1px solid #e3e3e3; padding:16px; border-radius:10px; margin-top:16px; }}
    .kpi-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:10px 0 0 0; }}
    .kpi {{ background:#fafafa; border:1px solid #eee; border-radius:10px; padding:10px 12px; min-width:240px; }}
    .kpi-title {{ font-size:12px; color:#666; margin-bottom:4px; }}
    .kpi-val {{ font-size:14px; font-weight:600; color:#222; }}
    .advisor-table {{ width:100%; border-collapse:collapse; margin-top:10px; }}
    .advisor-table th, .advisor-table td {{ padding:8px; border:1px solid #ddd; font-size:13px; }}
    .advisor-table th {{ background:#2c3e50; color:white; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size:12px; }}
    .note {{ font-size:12px; color:#555; margin-top:10px; }}
    .tiny {{ font-size:12px; color:#666; }}
  </style>
</head>
<body>
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p><strong>Generated:</strong> {esc(now)}</p>
  <p><strong>Old instances found:</strong> {len(old_gen_tracker)}</p>

  {advisor_html}

  <h2>Matched old instances</h2>
  <table>
    <tr>
      <th>Risk</th>
      <th>Shape</th>
      <th>Instance Name</th>
      <th>Lifecycle</th>
      <th>Compartment</th>
      <th>OCID</th>
      <th>Upgrade Options</th>
    </tr>
"""

    for row in old_gen_tracker:
        shape = row.get("shape", "") or ""
        risk = risk_for_shape(shape)
        row_class = "highrow" if risk == "High" else "medrow"

        comp_id = row.get("compartment_id", "")
        comp_name = comp_name_by_id.get(comp_id, "")
        comp_cell = f"{esc(comp_name)}<br/><span class='mono'>{esc(comp_id)}</span>" if comp_name else f"<span class='mono'>{esc(comp_id)}</span>"

        # Build upgrade options with ✅/❌ and reason
        targets = upgrade_targets_for_shape(shape)
        upgrades_lines: List[str] = []
        if not targets:
            upgrades_lines.append("<span class='tiny'>n/a</span>")
        else:
            for t in targets:
                ok, reason = evaluate_upgrade_option(
                    target_shape=t,
                    instance_ad=row.get("availability_domain"),
                    instance_compartment_id=comp_id,
                    shapes_cache_by_ad=shapes_cache_by_ad,
                    target_to_limits=target_to_limits,
                    limits_client=limits_client,
                    service_name=service_name,
                    ra_cache=ra_cache,
                )
                icon = "✅" if ok else "❌"
                # Keep it readable; show reason when ❌ (or when ok but catalog-only)
                if ok and reason == "catalog ok":
                    upgrades_lines.append(f"{esc(t)} {icon} <span class='tiny'>(quota unknown)</span>")
                elif ok:
                    upgrades_lines.append(f"{esc(t)} {icon}")
                else:
                    upgrades_lines.append(f"{esc(t)} {icon} <span class='tiny'>({esc(reason)})</span>")

        upgrades_html = "<br/>".join(upgrades_lines)

        html_content += f"""
    <tr class="{row_class}">
      <td><strong>{esc(risk)}</strong></td>
      <td class="mono">{esc(shape)}</td>
      <td>{esc(row.get('name',''))}</td>
      <td>{esc(row.get('lifecycle_state',''))}</td>
      <td>{comp_cell}</td>
      <td class="mono">{esc(row.get('ocid',''))}</td>
      <td>{upgrades_html}</td>
    </tr>
"""

    html_content += """
  </table>

  <p class="note">
    Instance-level ✅/❌ is based on AD shape catalog + quota/availability signals (when supported). If a target is not offered in the AD catalog (e.g., E6 missing in a region), it will show ❌ (not offered).
  </p>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️  HTML report saved to: {html_path}")


if __name__ == "__main__":
    main()
