#!/usr/bin/env python3
"""
👑 KING KAI — OCI Shapes Upgrade Report (no costs, split AMD/Intel, timestamped outputs)

Run:
  python3 oci_forgotten_resources_king_kai.py --shapes-upgrade-report

Outputs (auto-named, UTC timestamp):
  - king-kaiYYYYMMDD-HHMM.html
  - king-kaiYYYYMMDD-HHMM.csv

HTML:
  - No OCID and no compartment details
  - Two sections:
      AMD table columns:
        Risk, Shape, Instance Name, Lifecycle, VM.Standard.E5.Flex avail (✅/❌), VM.Standard.E6.Flex avail (✅/❌)
      Intel table columns:
        Risk, Shape, Instance Name, Lifecycle, VM.Standard3.Flex avail (✅/❌), VM.Optimized3.Flex avail (✅/❌)
  - Rows sorted by Shape (descending) within each table

CSV:
  - Includes OCID + compartment details for advanced use
"""

import oci
import argparse
import csv
import re
import sys
import html as htmlmod
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

# Upgrade targets required in HTML
E5_TARGET = "VM.Standard.E5.Flex"
E6_TARGET = "VM.Standard.E6.Flex"
STD3_TARGET = "VM.Standard3.Flex"
OPT3_TARGET = "VM.Optimized3.Flex"


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def esc(s: Any) -> str:
    if s is None:
        return ""
    return htmlmod.escape(str(s), quote=True)


def now_stamp_utc() -> str:
    # Matches: king-kaiYYYYMMDD-HHMM
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def collect_all_compartments(identity_client, tenancy_id: str) -> Tuple[List[str], Dict[str, str]]:
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
#  Classification + Risk
# ------------------------------------------------------------
def is_amd_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard\.E2\.\d+$", shape)) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex")


def is_intel_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard2\.\d+$", shape))


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


# ------------------------------------------------------------
#  Limits helpers (console-style signals)
# ------------------------------------------------------------
def discover_compute_service_name(limits_client, compartment_id: str) -> str:
    """
    Limits service uses a service_name string (often 'compute').
    Best-effort discovery.
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
    Dynamically find the correct limit names for each target shape family.
    We pick 1 core + 1 memory limit where possible.
    """
    if target_shape == E5_TARGET:
        prefixes = ["standard-e5"]
    elif target_shape == E6_TARGET:
        prefixes = ["standard-e6"]
    elif target_shape == STD3_TARGET:
        prefixes = ["standard3"]
    elif target_shape == OPT3_TARGET:
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


def evaluate_upgrade_option(
    target_shape: str,
    instance_ad: Optional[str],
    instance_compartment_id: str,
    shapes_cache_by_ad: Dict[str, Set[str]],
    target_to_limits: Dict[str, List[str]],
    limits_client,
    service_name: str,
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]],
) -> bool:
    """
    ✅ if:
      - AD known
      - target shape offered in AD catalog
      - AND if quota signals exist, they are not explicitly 0
    """
    if not instance_ad:
        return False

    ad_shapes = shapes_cache_by_ad.get(instance_ad, set())
    if target_shape not in ad_shapes:
        return False

    limit_names = target_to_limits.get(target_shape, [])
    if not limit_names:
        return True  # catalog ok

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
        if ra:
            eff = ra.get("effective_quota_value", None)
            avail = ra.get("available", None)
            if eff == 0 or avail == 0:
                return False

    return True


# ------------------------------------------------------------
#  Scan: shapes-only
# ------------------------------------------------------------
def scan_compartment_shapes_only(
    comp_id: str,
    compute_client,
    old_shapes_set: Optional[Set[str]],
    old_shape_regex: Optional[re.Pattern],
    out_rows: List[Dict[str, Any]],
) -> None:
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

        # Enrich with get_instance to reliably get AD
        ad = getattr(inst, "availability_domain", None)
        lifecycle = getattr(inst, "lifecycle_state", None)
        name = getattr(inst, "display_name", inst.id)
        ocid = getattr(inst, "id", None)

        try:
            full = compute_client.get_instance(inst.id).data
            ad = getattr(full, "availability_domain", ad)
            lifecycle = getattr(full, "lifecycle_state", lifecycle)
            name = getattr(full, "display_name", name)
        except Exception:
            pass

        out_rows.append({
            "name": name,
            "shape": shape,
            "availability_domain": ad,
            "compartment_id": comp_id,
            "lifecycle_state": lifecycle,
            "ocid": ocid,
            "risk": risk_for_shape(shape),
            "category": "AMD" if is_amd_old(shape) else ("Intel" if is_intel_old(shape) else "Other"),
        })


# ------------------------------------------------------------
#  HTML rendering
# ------------------------------------------------------------
def html_table_amd(rows: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    out.append("<h2>AMD instances</h2>")
    out.append("<table>")
    out.append(
        "<tr>"
        "<th>Risk</th>"
        "<th>Shape</th>"
        "<th>Instance Name</th>"
        "<th>Lifecycle</th>"
        f"<th>{esc(E5_TARGET)} avail</th>"
        f"<th>{esc(E6_TARGET)} avail</th>"
        "</tr>"
    )
    for r in rows:
        risk = r["risk"]
        row_class = "highrow" if risk == "High" else "medrow"
        out.append(
            f"<tr class='{row_class}'>"
            f"<td><strong>{esc(risk)}</strong></td>"
            f"<td class='mono'>{esc(r['shape'])}</td>"
            f"<td>{esc(r['name'])}</td>"
            f"<td>{esc(r.get('lifecycle_state',''))}</td>"
            f"<td style='text-align:center'>{esc(r.get('e5_icon','❌'))}</td>"
            f"<td style='text-align:center'>{esc(r.get('e6_icon','❌'))}</td>"
            "</tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def html_table_intel(rows: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    out.append("<h2>Intel instances</h2>")
    out.append("<table>")
    out.append(
        "<tr>"
        "<th>Risk</th>"
        "<th>Shape</th>"
        "<th>Instance Name</th>"
        "<th>Lifecycle</th>"
        f"<th>{esc(STD3_TARGET)} avail</th>"
        f"<th>{esc(OPT3_TARGET)} avail</th>"
        "</tr>"
    )
    for r in rows:
        risk = r["risk"]
        row_class = "highrow" if risk == "High" else "medrow"
        out.append(
            f"<tr class='{row_class}'>"
            f"<td><strong>{esc(risk)}</strong></td>"
            f"<td class='mono'>{esc(r['shape'])}</td>"
            f"<td>{esc(r['name'])}</td>"
            f"<td>{esc(r.get('lifecycle_state',''))}</td>"
            f"<td style='text-align:center'>{esc(r.get('std3_icon','❌'))}</td>"
            f"<td style='text-align:center'>{esc(r.get('opt3_icon','❌'))}</td>"
            "</tr>"
        )
    out.append("</table>")
    return "\n".join(out)


# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — Shapes Upgrade Report (timestamped, AMD/Intel split, no costs)")

    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name from ~/.oci/config (default: DEFAULT)")
    parser.add_argument("--output-dir", default=".", help="Directory to write reports (default: current directory)")

    parser.add_argument("--shapes-upgrade-report", action="store_true",
                        help="Scan predefined old shapes (AMD E2/E3/E4 + Intel Standard2).")
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

    stamp = now_stamp_utc()
    base_name = f"king-kai{stamp}"
    csv_path = f"{args.output_dir.rstrip('/')}/{base_name}.csv"
    html_path = f"{args.output_dir.rstrip('/')}/{base_name}.html"

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

    all_rows: List[Dict[str, Any]] = []
    for comp_id in compartments:
        scan_compartment_shapes_only(
            comp_id=comp_id,
            compute_client=compute_client,
            old_shapes_set=old_shapes_set,
            old_shape_regex=old_shape_regex,
            out_rows=all_rows,
        )

    # Build caches for upgrade availability checks
    # 1) AD catalog shapes cache
    ads_to_check: Set[str] = set([r["availability_domain"] for r in all_rows if r.get("availability_domain")]) or set(availability_domains)
    shapes_cache_by_ad: Dict[str, Set[str]] = {}
    for ad in sorted([a for a in ads_to_check if a]):
        shapes_cache_by_ad[ad] = list_shapes_in_ad(compute_client, tenancy_id, ad)

    # 2) Limits: map targets -> limit names
    service_name = discover_compute_service_name(limits_client, tenancy_id)
    limit_index = build_limit_value_index(limits_client, tenancy_id, service_name)
    all_limit_names = {k[0] for k in limit_index.keys() if k and k[0]}

    targets = [E5_TARGET, E6_TARGET, STD3_TARGET, OPT3_TARGET]
    target_to_limits: Dict[str, List[str]] = {t: find_limit_names_for_target(all_limit_names, t) for t in targets}

    # 3) Resource availability cache
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]] = {}

    # Compute per-instance icons
    for r in all_rows:
        ad = r.get("availability_domain")
        comp_id = r.get("compartment_id", "")

        if is_amd_old(r["shape"]):
            e5_ok = evaluate_upgrade_option(E5_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            e6_ok = evaluate_upgrade_option(E6_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            r["e5_icon"] = "✅" if e5_ok else "❌"
            r["e6_icon"] = "✅" if e6_ok else "❌"

        if is_intel_old(r["shape"]):
            std3_ok = evaluate_upgrade_option(STD3_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            opt3_ok = evaluate_upgrade_option(OPT3_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            r["std3_icon"] = "✅" if std3_ok else "❌"
            r["opt3_icon"] = "✅" if opt3_ok else "❌"

    # Split + sort for HTML (Shape descending)
    amd_rows = sorted([r for r in all_rows if r["category"] == "AMD"], key=lambda x: x["shape"], reverse=True)
    intel_rows = sorted([r for r in all_rows if r["category"] == "Intel"], key=lambda x: x["shape"], reverse=True)

    # --------------- Generate CSV (keeps OCID + compartment details) ----------------
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "Category", "Risk", "Shape", "InstanceName", "Lifecycle",
            "AvailabilityDomain",
            "CompartmentName", "CompartmentId", "OCID",
            f"{E5_TARGET}_avail", f"{E6_TARGET}_avail",
            f"{STD3_TARGET}_avail", f"{OPT3_TARGET}_avail",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for r in all_rows:
            comp_id = r.get("compartment_id", "")
            writer.writerow({
                "Category": r.get("category", ""),
                "Risk": r.get("risk", ""),
                "Shape": r.get("shape", ""),
                "InstanceName": r.get("name", ""),
                "Lifecycle": r.get("lifecycle_state", ""),
                "AvailabilityDomain": r.get("availability_domain", ""),
                "CompartmentName": comp_name_by_id.get(comp_id, ""),
                "CompartmentId": comp_id,
                "OCID": r.get("ocid", ""),
                f"{E5_TARGET}_avail": r.get("e5_icon", ""),
                f"{E6_TARGET}_avail": r.get("e6_icon", ""),
                f"{STD3_TARGET}_avail": r.get("std3_icon", ""),
                f"{OPT3_TARGET}_avail": r.get("opt3_icon", ""),
            })

    print(f"🗒️ CSV report saved to: {csv_path}")

    # --------------- Generate HTML (no OCID + no compartment details) ----------------
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>KING KAI — Shapes Upgrade Report</title>
  <style>
    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f9f9f9; margin: 20px; }}
    h1 {{ color:#2c3e50; font-size:28px; margin-bottom: 6px; }}
    p {{ font-size:14px; }}

    table {{ width:100%; border-collapse:collapse; margin-top:14px; }}
    th, td {{ padding:10px; border:1px solid #ddd; text-align:left; font-size:13px; vertical-align: top; }}
    th {{ background:#34495e; color:white; }}
    tr:nth-child(even) {{ background:#f2f2f2; }}

    .highrow {{ background: #fdecea !important; }}   /* light red */
    .medrow  {{ background: #fff4e5 !important; }}   /* light orange */

    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size:12px; }}
    .note {{ font-size:12px; color:#555; margin-top:10px; }}
  </style>
</head>
<body>
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p><strong>Generated:</strong> {esc(generated)}</p>
  <p><strong>AMD old instances:</strong> {len(amd_rows)} &nbsp; | &nbsp; <strong>Intel old instances:</strong> {len(intel_rows)}</p>
  <p class="note">HTML excludes OCID + compartment details. Use the CSV for advanced operations.</p>

  {html_table_amd(amd_rows) if amd_rows else "<h2>AMD instances</h2><p class='note'>No AMD old instances found.</p>"}
  {html_table_intel(intel_rows) if intel_rows else "<h2>Intel instances</h2><p class='note'>No Intel old instances found.</p>"}

</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️ HTML report saved to: {html_path}")
    print(f"✅ Done. Output prefix: {base_name}")


if __name__ == "__main__":
    main()
