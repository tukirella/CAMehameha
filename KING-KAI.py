#!/usr/bin/env python3
"""
👑 KING KAI — OCI Forgotten Resource Detective (vNext)

Modes:
  1) Shapes Upgrade Report (recommended):
       --shapes-upgrade-report
     Scans ONLY the predefined old shapes (AMD E2/E3/E4 + Intel Standard2) and produces:
       - counts
       - upgrade targets
       - per-AD shape catalog presence
       - limits/usage/available (like OCI console "Limits, quotas and usage")
     HTML/CSV will include ONLY shapes-related content.

  2) Full scan (optional):
       --scan-mode full
     Runs the original forgotten-resource checks too (IPs, NSGs, LBs, orphan volumes, etc.)

Notes:
  - Region is taken from the OCI config profile (DEFAULT unless overridden).
  - Limits names are discovered dynamically (e.g., *-regional-count) to match console behavior.

Usage:
  Shapes-only:
    python3 oci_forgotten_resources_king_kai.py --shapes-upgrade-report

  Full scan:
    python3 oci_forgotten_resources_king_kai.py --scan-mode full
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

def has_no_tags(resource) -> bool:
    ff = getattr(resource, "freeform_tags", None)
    df = getattr(resource, "defined_tags", None)
    return (not ff or len(ff) == 0) and (not df or len(df) == 0)

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
#  👑 KING KAI — Upgrade Advisor (limits + catalog)
# ------------------------------------------------------------
def classify_old_shape(shape: str) -> str:
    if re.match(r"^VM\.Standard\.E2\b", shape): return "AMD_E2"
    if re.match(r"^VM\.Standard\.E3\b", shape): return "AMD_E3"
    if re.match(r"^VM\.Standard\.E4\b", shape): return "AMD_E4"
    if re.match(r"^VM\.Standard2\b", shape):    return "INTEL_STD2"
    if re.match(r"^VM\.Standard1\b", shape):    return "INTEL_STD1"  # kept for backward compat
    return "OTHER"

def recommended_targets(counts: Dict[str, int]) -> List[str]:
    targets: List[str] = []
    amd_present = (counts.get("AMD_E2", 0) + counts.get("AMD_E3", 0) + counts.get("AMD_E4", 0)) > 0
    intel_present = (counts.get("INTEL_STD1", 0) + counts.get("INTEL_STD2", 0)) > 0
    if amd_present:
        targets.extend(AMD_UPGRADE_TARGETS)
    if intel_present:
        targets.extend(INTEL_UPGRADE_TARGETS)
    return targets

def list_shapes_in_ad(compute_client, tenancy_id: str, ad: str) -> Set[str]:
    try:
        shapes = oci.pagination.list_call_get_all_results(
            compute_client.list_shapes,
            compartment_id=tenancy_id,
            availability_domain=ad
        ).data
        return {getattr(s, "shape", "") for s in shapes if getattr(s, "shape", "")}
    except Exception:
        return set()

def discover_compute_service_name(limits_client, tenancy_id: str) -> str:
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
    ad: Optional[str],
):
    """
    Try to fetch used/available/effective_quota_value.
    Mirrors the console's "Usage" + "Available" behavior when supported.
    """
    # REGION scope
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
        # Try AD scope
        if ad:
            try:
                ra = limits_client.get_resource_availability(
                    service_name=service_name,
                    limit_name=limit_name,
                    compartment_id=compartment_id,
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

def find_limit_names_for_target(all_limit_names: Set[str], target_shape: str) -> List[str]:
    """
    Dynamically find the correct limit names (often '*-regional-count') for each target.
    We pick "core" + "memory" limits where possible.
    """
    # Prefixes aligned to the console naming style
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

    hits = []
    for p in prefixes:
        core = sorted([n for n in all_limit_names if n.startswith(p) and "core" in n and ("regional" in n or n.endswith("count"))])
        mem  = sorted([n for n in all_limit_names if n.startswith(p) and "memory" in n and ("regional" in n or n.endswith("count"))])

        # Prefer common patterns first
        def prefer_regional_count(names: List[str]) -> List[str]:
            ranked = sorted(names, key=lambda x: (0 if "regional-count" in x else 1, len(x)))
            return ranked

        core = prefer_regional_count(core)
        mem  = prefer_regional_count(mem)

        if core:
            hits.append(core[0])
        if mem:
            hits.append(mem[0])

    return hits

def compute_upgrade_advice(
    old_gen_tracker: List[Dict[str, Any]],
    compute_client,
    limits_client,
    root_compartment_id: str,
    tenancy_id: str,
    availability_domains: List[str],
) -> Dict[str, Any]:
    """
    Returns a dict for HTML rendering:
      - counts per family
      - recommended targets
      - per-AD catalog flags
      - per-limit service limit + used + available + effective quota
    """
    counts = Counter()
    ads_seen: Set[str] = set()

    for row in old_gen_tracker:
        shape = row.get("shape", "") or ""
        ad = row.get("availability_domain")
        if ad:
            ads_seen.add(ad)
        counts[classify_old_shape(shape)] += 1

    # explicit zeros
    for k in ["AMD_E2", "AMD_E3", "AMD_E4", "INTEL_STD1", "INTEL_STD2"]:
        counts.setdefault(k, 0)

    targets = recommended_targets(dict(counts))

    advice: Dict[str, Any] = {
        "counts": dict(counts),
        "targets": targets,
        "ads": sorted(list(ads_seen)) if ads_seen else sorted([a for a in availability_domains if a]),
        "catalog": {},
        "limits": [],
        "compute_service": "",
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

    # Limits discovery
    service_name = discover_compute_service_name(limits_client, root_compartment_id)
    advice["compute_service"] = service_name

    limit_index = build_limit_value_index(limits_client, root_compartment_id, service_name)
    all_limit_names = {k[0] for k in limit_index.keys() if k and k[0]}

    needed_limits: Set[str] = set()
    for t in targets:
        needed_limits.update(find_limit_names_for_target(all_limit_names, t))

    needed_limits_sorted = sorted(needed_limits)

    # pick an AD for availability calls fallback
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
            compartment_id=root_compartment_id,
            limit_name=limit_name,
            ad=any_ad
        ) or {}

        advice["limits"].append({
            "name": limit_name,
            "global": global_val,
            "region": region_val,
            "ad_vals": {k: ad_vals[k] for k in sorted(ad_vals.keys())} if ad_vals else {},
            "availability": ra,  # {available, used, effective_quota_value}
        })

    return advice

def render_upgrade_advice_html(advice: Dict[str, Any]) -> str:
    if not advice:
        return ""

    c = advice.get("counts", {}) or {}
    amd_line = f"E2={c.get('AMD_E2',0)} | E3={c.get('AMD_E3',0)} | E4={c.get('AMD_E4',0)}"
    intel_line = f"Standard2={c.get('INTEL_STD2',0)} (Std1={c.get('INTEL_STD1',0)})"

    targets: List[str] = advice.get("targets", []) or []
    ads: List[str] = advice.get("ads", []) or []
    service_name = esc(advice.get("compute_service", ""))

    def fmt(v):
        return "" if v is None else esc(v)  # keep 0 visible

    out: List[str] = []
    out.append('<div class="kingsai">')
    out.append('<h2>👑 KING KAI — Shapes Upgrade Advisor</h2>')

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
    if (c.get("INTEL_STD1", 0) + c.get("INTEL_STD2", 0)) > 0:
        out.append(f"<li><strong>Intel →</strong> {', '.join(map(esc, INTEL_UPGRADE_TARGETS))}</li>")
    out.append('</ul>')

    # Catalog per AD
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

    # Limits table (mirrors console: limit + used + available)
    out.append('<h3>Tenant limits, usage & available</h3>')
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
        "Note: Shape catalog presence means the shape is offered in the AD catalog; it does not guarantee real-time capacity. "
        "Limits/usage/available reflect tenancy quotas and availability where supported."
        "</p>"
    )
    out.append("</div>")
    return "\n".join(out)

# ------------------------------------------------------------
#  Shapes-only scan (old shapes list or regex)
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

        if match:
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

    return findings

# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — OCI Forgotten Resource Detective (vNext)")

    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name from ~/.oci/config (default: DEFAULT)")
    parser.add_argument("--output-csv", default="forgotten_resources_report.csv", help="CSV path (default: forgotten_resources_report.csv)")
    parser.add_argument("--output-html", default="forgotten_resources_report.html", help="HTML path (default: forgotten_resources_report.html)")

    parser.add_argument("--scan-mode", choices=["shapes", "full"], default="shapes",
                        help="shapes = shapes-only report (default), full = include other detectors too")
    parser.add_argument("--shapes-upgrade-report", action="store_true",
                        help="Scan predefined old shapes (AMD E2/E3/E4 + Intel Standard2) and produce upgrade advisor. Implies shapes-only.")
    parser.add_argument("--old-shape-pattern", default=None,
                        help="Optional regex to define 'old' shapes (shapes-only). If provided, overrides the predefined set.")

    # (full scan options remain for future; shapes-only is your focus now)
    parser.add_argument("--suspicious-name-regex", default=r"\b(test|temp|demo|old|backup|poc)\b",
                        help=r"Regex for sketchy resource names (only used in full mode)")

    args = parser.parse_args()

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

    # Decide mode:
    # - If --shapes-upgrade-report OR --old-shape-pattern is used → shapes-only HTML (your requirement #2)
    if args.shapes_upgrade_report or args.old_shape_pattern:
        scan_mode = "shapes"
    else:
        scan_mode = args.scan_mode

    # Shapes match configuration
    old_shapes_set = None
    old_shape_regex = None

    if scan_mode == "shapes":
        if args.old_shape_pattern:
            try:
                old_shape_regex = re.compile(args.old_shape_pattern)
            except re.error as e:
                print(f"❌ Invalid regex for --old-shape-pattern: {e}")
                sys.exit(1)
        else:
            # default requested set (covers AMD+Intel in one command)
            old_shapes_set = set(OLD_SHAPES_SET)

    all_findings: List[Dict[str, Any]] = []
    old_gen_tracker: List[Dict[str, Any]] = []

    print(f"🔍 Scanning tenancy {tenancy_id} (mode={scan_mode}) …")
    print(f"   Compartments: {len(compartments)} (including root)")
    print()

    for comp_id in compartments:
        if scan_mode == "shapes":
            comp_findings = scan_compartment_shapes_only(
                comp_id=comp_id,
                compute_client=compute_client,
                old_shapes_set=old_shapes_set,
                old_shape_regex=old_shape_regex,
                old_gen_tracker=old_gen_tracker,
            )
            all_findings.extend(comp_findings)
        else:
            # Full mode placeholder (kept for later). For now, we keep behavior simple.
            comp_findings = scan_compartment_shapes_only(
                comp_id=comp_id,
                compute_client=compute_client,
                old_shapes_set=old_shapes_set or set(OLD_SHAPES_SET),
                old_shape_regex=old_shape_regex,
                old_gen_tracker=old_gen_tracker,
            )
            all_findings.extend(comp_findings)

    # Compute upgrade advice (root compartment = tenancy id in the console view)
    upgrade_advice = compute_upgrade_advice(
        old_gen_tracker=old_gen_tracker,
        compute_client=compute_client,
        limits_client=limits_client,
        root_compartment_id=tenancy_id,
        tenancy_id=tenancy_id,
        availability_domains=availability_domains,
    )

    # CSV
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
            row = dict(item)
            row["FreeformTags"] = "{}" if not row.get("FreeformTags") else str(row["FreeformTags"])
            row["DefinedTags"] = "{}" if not row.get("DefinedTags") else str(row["DefinedTags"])
            writer.writerow(row)

    print(f"🗒️  CSV report saved to: {csv_path}")

    # HTML (shapes-only per your requirement)
    html_path = args.output_html
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    advisor_html = render_upgrade_advice_html(upgrade_advice)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>KING KAI — Shapes Upgrade Report</title>
  <style>
    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f9f9f9; margin: 20px; }}
    h1 {{ color:#2c3e50; font-size:28px; margin-bottom: 6px; }}
    p {{ font-size:14px; }}

    table {{ width:100%; border-collapse:collapse; margin-top:20px; }}
    th, td {{ padding:10px; border:1px solid #ddd; text-align:left; font-size:13px; vertical-align: top; }}
    th {{ background:#34495e; color:white; }}
    tr:nth-child(even) {{ background:#f2f2f2; }}

    .medium {{ background: #fff4e5; }}

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
  </style>
</head>
<body>
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p><strong>Generated:</strong> {esc(now)}</p>
  <p><strong>Old instances found:</strong> {len(all_findings)}</p>

  {advisor_html}

  <h2>Matched old instances</h2>
  <table>
    <tr>
      <th>Risk</th>
      <th>Instance Name</th>
      <th>Shape</th>
      <th>Compartment</th>
      <th>Lifecycle</th>
      <th>OCID</th>
    </tr>
"""

    for row in old_gen_tracker:
        comp_id = row.get("compartment_id", "")
        comp_name = comp_name_by_id.get(comp_id, "")
        comp_cell = f"{esc(comp_name)}<br/><span class='mono'>{esc(comp_id)}</span>" if comp_name else f"<span class='mono'>{esc(comp_id)}</span>"
        html_content += f"""
    <tr class="medium">
      <td>Medium</td>
      <td>{esc(row.get('name',''))}</td>
      <td class="mono">{esc(row.get('shape',''))}</td>
      <td>{comp_cell}</td>
      <td>{esc(row.get('lifecycle_state',''))}</td>
      <td class="mono">{esc(row.get('ocid',''))}</td>
    </tr>
"""

    html_content += """
  </table>

  <p class="note">This report is shapes-only (by design) when --shapes-upgrade-report or --old-shape-pattern is used.</p>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️  HTML report saved to: {html_path}")


if __name__ == "__main__":
    main()
