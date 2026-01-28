#!/usr/bin/env python3
"""
👑 KING KAI — Shapes Upgrade Report (timestamped outputs, AMD/Intel split, +Creator + Pricing/Deltas)

Run:
  python3 king-kai.py

Outputs (auto-named, UTC timestamp):
  - king-kaiYYYYMMDD-HHMM.html
  - king-kaiYYYYMMDD-HHMM.csv

HTML (NO OCID/Compartment details):
  - Executive intro summary (counts + pricing disclaimer)
  - Two sections (tables):
      AMD table columns (order):
        Risk, oCPU, Memory [GB], Shape, Instance Name, Creator, Lifecycle, Current Cost/mo,
        VM.Standard.E5.Flex avail (✅/❌), VM.Standard.E6.Flex avail (✅/❌),
        VM.Standard.E5/E6.Flex delta add-on $/mo.   (combined, right-most column)

      Intel table columns (order):
        Risk, oCPU, Memory [GB], Shape, Instance Name, Creator, Lifecycle, Current Cost/mo,
        VM.Standard3.Flex avail (✅/❌), VM.Optimized3.Flex avail (✅/❌),
        (Intel keeps its own deltas per-target in CSV; HTML stays aligned to AMD request)

  - Rows are sorted by Shape (descending) within each table

CSV (includes OCID + compartment details for advanced use):
  - Includes all columns + AvailabilityDomain, CompartmentName, CompartmentId, OCID.

Adjustments implemented from your request:
  1) Script name: king-kai.py (usage updated)
  2) No required flags; runs as-is
  3) AMD E5/E6 deltas combined into one column (right-most)
  4) Executive intro block restored + pricing disclaimer: "Jan-2026 OCI pricing list baseline"
  5) Risk "Critical" for VM.Standard.E2.1
  6) New column "Creator" (best-effort from defined/freeform tags: createdBy/creator/CreatedBy/Owner)
  7) HTML column order updated as requested (AMD); Intel table also includes Creator and the same core order

Important:
  - "Creator" relies on tags. If no creator-like tag exists, it will show "Unknown".
  - Availability checks are best-effort: shape catalog + quota signals (if available) for the compartment/AD.
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

# Upgrade targets required
E5_TARGET = "VM.Standard.E5.Flex"
E6_TARGET = "VM.Standard.E6.Flex"
STD3_TARGET = "VM.Standard3.Flex"
OPT3_TARGET = "VM.Optimized3.Flex"


# ------------------------------------------------------------
#  Pricing baseline (Monthly USD) — from your OCI Calculator table
# ------------------------------------------------------------
FLEX_BASE_OCPU = 1.0
FLEX_BASE_MEM_GB = 8.0

AMD_FLEX_PRICING = {
    "E2": {"base": 27.0, "extra_ocpu": 14.5, "extra_gb": 1.0},
    "E3": {"base": 27.0, "extra_ocpu": 18.0, "extra_gb": 1.0},
    "E4": {"base": 27.0, "extra_ocpu": 20.0, "extra_gb": 1.0},
    "E5": {"base": 34.0, "extra_ocpu": 22.5, "extra_gb": 1.5},
    "E6": {"base": 34.0, "extra_ocpu": 22.5, "extra_gb": 1.5},
}

INTEL_FLEX_PRICING = {
    "STD3": {"base": 39.0, "extra_ocpu": 30.0, "extra_gb": 1.1},
    "OPT3": {"base": 49.0, "extra_ocpu": 40.0, "extra_gb": 1.1},
}

# Intel Standard2 fixed presets (oCPU, MemoryGB, MonthlyCostUSD) — from your table
INTEL_STANDARD2_FIXED = {
    "VM.Standard2.1":  {"ocpu": 1.0,  "mem_gb": 8.0,   "cost": 47.50},
    "VM.Standard2.2":  {"ocpu": 2.0,  "mem_gb": 30.0,  "cost": 95.00},
    "VM.Standard2.4":  {"ocpu": 4.0,  "mem_gb": 60.0,  "cost": 190.00},
    "VM.Standard2.8":  {"ocpu": 8.0,  "mem_gb": 120.0, "cost": 380.00},
    "VM.Standard2.16": {"ocpu": 16.0, "mem_gb": 240.0, "cost": 760.00},
    "VM.Standard2.24": {"ocpu": 24.0, "mem_gb": 320.0, "cost": 1140.00},
}

# AMD E2 fixed shapes memory mapping (GB)
E2_FIXED_MEM_GB = {
    "VM.Standard.E2.1": 8.0,
    "VM.Standard.E2.2": 16.0,
    "VM.Standard.E2.4": 32.0,
    "VM.Standard.E2.8": 64.0,
}


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def esc(s: Any) -> str:
    if s is None:
        return ""
    return htmlmod.escape(str(s), quote=True)


def now_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "Unknown"
    if abs(v - int(v)) < 1e-9:
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "Unknown"
    return f"${v:,.2f}"


def fmt_delta(v: Optional[float]) -> str:
    if v is None:
        return "Unknown"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def collect_all_compartments(identity_client, tenancy_id: str) -> Tuple[List[str], Dict[str, str]]:
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


def get_creator_from_tags(freeform: Optional[Dict[str, Any]], defined: Optional[Dict[str, Any]]) -> str:
    """
    Best-effort "Creator" from tags.
    Looks for common keys in freeform + defined tags.
    """
    candidates = {
        "creator", "createdby", "created_by", "created-by", "createdBy",
        "owner", "created", "createdByEmail", "created_by_email"
    }

    ff = freeform or {}
    df = defined or {}

    # defined tags structure: {namespace: {key: value}}
    def iter_defined():
        for ns, d in df.items():
            if isinstance(d, dict):
                for k, v in d.items():
                    yield k, v

    # freeform first
    for k, v in ff.items():
        if str(k).strip().lower() in candidates and v:
            return str(v)

    # defined tags next
    for k, v in iter_defined():
        if str(k).strip().lower() in candidates and v:
            return str(v)

    return "Unknown"


# ------------------------------------------------------------
#  Classification + Risk
# ------------------------------------------------------------
def is_amd_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard\.E2\.\d+$", shape)) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex")


def is_intel_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard2\.\d+$", shape))


def risk_for_shape(shape: str) -> str:
    """
    CRITICAL:
      - VM.Standard.E2.1
    HIGH:
      - AMD VM.Standard.E2.*
      - AMD VM.Standard.E3.Flex
    MEDIUM:
      - everything else in old list
    """
    if shape == "VM.Standard.E2.1":
        return "Critical"
    if re.match(r"^VM\.Standard\.E2\.\d+$", shape):
        return "High"
    if shape == "VM.Standard.E3.Flex":
        return "High"
    return "Medium"


# ------------------------------------------------------------
#  Cost engine (baseline from your calculator)
# ------------------------------------------------------------
def linear_flex_cost(model: Dict[str, float], ocpu: Optional[float], mem_gb: Optional[float]) -> Optional[float]:
    if ocpu is None or mem_gb is None:
        return None
    base = model["base"]
    extra_ocpu = model["extra_ocpu"]
    extra_gb = model["extra_gb"]
    cost = base + (ocpu - FLEX_BASE_OCPU) * extra_ocpu + (mem_gb - FLEX_BASE_MEM_GB) * extra_gb
    return round(cost, 2)


def current_monthly_cost(shape: str, ocpu: Optional[float], mem_gb: Optional[float]) -> Optional[float]:
    if shape in INTEL_STANDARD2_FIXED:
        return float(INTEL_STANDARD2_FIXED[shape]["cost"])

    if re.match(r"^VM\.Standard\.E2\.\d+$", shape):
        return linear_flex_cost(AMD_FLEX_PRICING["E2"], ocpu, mem_gb)
    if shape == "VM.Standard.E3.Flex":
        return linear_flex_cost(AMD_FLEX_PRICING["E3"], ocpu, mem_gb)
    if shape == "VM.Standard.E4.Flex":
        return linear_flex_cost(AMD_FLEX_PRICING["E4"], ocpu, mem_gb)

    # best-effort if regex includes these
    if shape == E5_TARGET:
        return linear_flex_cost(AMD_FLEX_PRICING["E5"], ocpu, mem_gb)
    if shape == E6_TARGET:
        return linear_flex_cost(AMD_FLEX_PRICING["E6"], ocpu, mem_gb)
    if shape == STD3_TARGET:
        return linear_flex_cost(INTEL_FLEX_PRICING["STD3"], ocpu, mem_gb)
    if shape == OPT3_TARGET:
        return linear_flex_cost(INTEL_FLEX_PRICING["OPT3"], ocpu, mem_gb)

    return None


def target_monthly_cost(target_shape: str, ocpu: Optional[float], mem_gb: Optional[float]) -> Optional[float]:
    if target_shape == E5_TARGET:
        return linear_flex_cost(AMD_FLEX_PRICING["E5"], ocpu, mem_gb)
    if target_shape == E6_TARGET:
        return linear_flex_cost(AMD_FLEX_PRICING["E6"], ocpu, mem_gb)
    if target_shape == STD3_TARGET:
        return linear_flex_cost(INTEL_FLEX_PRICING["STD3"], ocpu, mem_gb)
    if target_shape == OPT3_TARGET:
        return linear_flex_cost(INTEL_FLEX_PRICING["OPT3"], ocpu, mem_gb)
    return None


# ------------------------------------------------------------
#  Infer oCPU & Memory (GB) from instance
# ------------------------------------------------------------
def infer_ocpu_mem(shape: str, shape_config: Optional[Any]) -> Tuple[Optional[float], Optional[float]]:
    if shape in INTEL_STANDARD2_FIXED:
        e = INTEL_STANDARD2_FIXED[shape]
        return float(e["ocpu"]), float(e["mem_gb"])

    if shape in E2_FIXED_MEM_GB:
        try:
            ocpu = float(shape.split(".")[-1])
        except Exception:
            ocpu = None
        return ocpu, float(E2_FIXED_MEM_GB[shape])

    if shape_config is not None:
        ocpus = getattr(shape_config, "ocpus", None)
        mem = getattr(shape_config, "memory_in_gbs", None)
        if ocpus is not None and mem is not None:
            try:
                return float(ocpus), float(mem)
            except Exception:
                pass

    return None, None


# ------------------------------------------------------------
#  Availability checks (shape catalog + quota signals)
# ------------------------------------------------------------
def discover_compute_service_name(limits_client, compartment_id: str) -> str:
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


def list_limit_names(limits_client, compartment_id: str, service_name: str) -> Set[str]:
    names: Set[str] = set()
    try:
        vals = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=compartment_id,
            service_name=service_name
        ).data
        for lv in vals:
            n = getattr(lv, "name", None)
            if n:
                names.add(str(n))
    except Exception:
        pass
    return names


def get_resource_availability_safe(
    limits_client,
    service_name: str,
    compartment_id: str,
    limit_name: str,
    availability_domain: Optional[str],
) -> Optional[Dict[str, Any]]:
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
    if not instance_ad:
        return False

    ad_shapes = shapes_cache_by_ad.get(instance_ad, set())
    if target_shape not in ad_shapes:
        return False

    limit_names = target_to_limits.get(target_shape, [])
    if not limit_names:
        return True

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
#  Scan: shapes-only (enrich with Creator, costs, deltas)
# ------------------------------------------------------------
def scan_compartment_shapes_only(
    comp_id: str,
    compute_client,
    out_rows: List[Dict[str, Any]],
) -> None:
    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=comp_id
    ).data

    for inst in instances:
        shape = getattr(inst, "shape", "") or ""
        if shape not in OLD_SHAPES_SET:
            continue

        ad = getattr(inst, "availability_domain", None)
        lifecycle = getattr(inst, "lifecycle_state", None)
        name = getattr(inst, "display_name", inst.id)
        ocid = getattr(inst, "id", None)
        shape_config = getattr(inst, "shape_config", None)
        ff_tags = getattr(inst, "freeform_tags", None)
        df_tags = getattr(inst, "defined_tags", None)

        try:
            full = compute_client.get_instance(inst.id).data
            ad = getattr(full, "availability_domain", ad)
            lifecycle = getattr(full, "lifecycle_state", lifecycle)
            name = getattr(full, "display_name", name)
            shape_config = getattr(full, "shape_config", shape_config)
            ff_tags = getattr(full, "freeform_tags", ff_tags)
            df_tags = getattr(full, "defined_tags", df_tags)
        except Exception:
            pass

        ocpus, mem_gb = infer_ocpu_mem(shape, shape_config)
        cur_cost = current_monthly_cost(shape, ocpus, mem_gb)
        creator = get_creator_from_tags(ff_tags, df_tags)

        out_rows.append({
            "name": name,
            "shape": shape,
            "availability_domain": ad,
            "compartment_id": comp_id,
            "lifecycle_state": lifecycle,
            "ocid": ocid,
            "risk": risk_for_shape(shape),
            "category": "AMD" if is_amd_old(shape) else ("Intel" if is_intel_old(shape) else "Other"),
            "ocpus": ocpus,
            "mem_gb": mem_gb,
            "current_cost": cur_cost,
            "creator": creator,
        })


# ------------------------------------------------------------
#  HTML rendering
# ------------------------------------------------------------
def css_for_risk(risk: str) -> str:
    if risk == "Critical":
        return "critrow"
    if risk == "High":
        return "highrow"
    return "medrow"


def html_table_amd(rows: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    out.append("<h2>AMD old instances</h2>")
    out.append("<table>")
    out.append(
        "<tr>"
        "<th>Risk</th>"
        "<th>oCPU</th>"
        "<th>Memory [GB]</th>"
        "<th>Shape</th>"
        "<th>Instance Name</th>"
        "<th>Creator</th>"
        "<th>Lifecycle</th>"
        "<th>Current Cost/mo</th>"
        f"<th>{esc(E5_TARGET)} avail</th>"
        f"<th>{esc(E6_TARGET)} avail</th>"
        "<th>VM.Standard.E5/E6.Flex delta add-on $/mo.</th>"
        "</tr>"
    )

    for r in rows:
        risk = r["risk"]
        row_class = css_for_risk(risk)
        out.append(
            f"<tr class='{row_class}'>"
            f"<td><strong>{esc(risk)}</strong></td>"
            f"<td class='num'>{esc(fmt_num(r.get('ocpus')))}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('mem_gb')))}</td>"
            f"<td class='mono'>{esc(r['shape'])}</td>"
            f"<td>{esc(r['name'])}</td>"
            f"<td>{esc(r.get('creator','Unknown'))}</td>"
            f"<td>{esc(r.get('lifecycle_state',''))}</td>"
            f"<td class='num'>{esc(fmt_money(r.get('current_cost')))}</td>"
            f"<td class='center'>{esc(r.get('e5_icon','❌'))}</td>"
            f"<td class='center'>{esc(r.get('e6_icon','❌'))}</td>"
            f"<td class='num'>{esc(fmt_delta(r.get('e56_delta')))}</td>"
            "</tr>"
        )

    out.append("</table>")
    return "\n".join(out)


def html_table_intel(rows: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    out.append("<h2>Intel old instances</h2>")
    out.append("<table>")
    out.append(
        "<tr>"
        "<th>Risk</th>"
        "<th>oCPU</th>"
        "<th>Memory [GB]</th>"
        "<th>Shape</th>"
        "<th>Instance Name</th>"
        "<th>Creator</th>"
        "<th>Lifecycle</th>"
        "<th>Current Cost/mo</th>"
        f"<th>{esc(STD3_TARGET)} avail</th>"
        f"<th>{esc(OPT3_TARGET)} avail</th>"
        "<th>Upgrade delta add-on $/mo. (best option)</th>"
        "</tr>"
    )

    for r in rows:
        risk = r["risk"]
        row_class = css_for_risk(risk)
        out.append(
            f"<tr class='{row_class}'>"
            f"<td><strong>{esc(risk)}</strong></td>"
            f"<td class='num'>{esc(fmt_num(r.get('ocpus')))}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('mem_gb')))}</td>"
            f"<td class='mono'>{esc(r['shape'])}</td>"
            f"<td>{esc(r['name'])}</td>"
            f"<td>{esc(r.get('creator','Unknown'))}</td>"
            f"<td>{esc(r.get('lifecycle_state',''))}</td>"
            f"<td class='num'>{esc(fmt_money(r.get('current_cost')))}</td>"
            f"<td class='center'>{esc(r.get('std3_icon','❌'))}</td>"
            f"<td class='center'>{esc(r.get('opt3_icon','❌'))}</td>"
            f"<td class='num'>{esc(fmt_delta(r.get('best_intel_delta')))}</td>"
            "</tr>"
        )

    out.append("</table>")
    return "\n".join(out)


# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — Shapes Upgrade Report (no flags required)")
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name from ~/.oci/config (default: DEFAULT)")
    parser.add_argument("--output-dir", default=".", help="Directory to write reports (default: current directory)")
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

    stamp = now_stamp_utc()
    base_name = f"king-kai{stamp}"
    out_dir = args.output_dir.rstrip("/")
    csv_path = f"{out_dir}/{base_name}.csv"
    html_path = f"{out_dir}/{base_name}.html"

    compartments, comp_name_by_id = collect_all_compartments(identity_client, tenancy_id)
    availability_domains = list_availability_domains(identity_client, tenancy_id)

    print(f"🔍 KING KAI scanning tenancy {tenancy_id} (legacy shapes only)…")
    print(f"   Compartments: {len(compartments)} (including root)")
    print()

    all_rows: List[Dict[str, Any]] = []
    for comp_id in compartments:
        scan_compartment_shapes_only(comp_id, compute_client, all_rows)

    # Executive counts
    amd_counts = {"E2": 0, "E3": 0, "E4": 0}
    intel_counts = {"Standard2": 0}
    for r in all_rows:
        if r.get("category") == "AMD":
            sh = r.get("shape", "")
            if sh.startswith("VM.Standard.E2."):
                amd_counts["E2"] += 1
            elif sh == "VM.Standard.E3.Flex":
                amd_counts["E3"] += 1
            elif sh == "VM.Standard.E4.Flex":
                amd_counts["E4"] += 1
        elif r.get("category") == "Intel":
            intel_counts["Standard2"] += 1

    # Build caches for upgrade availability checks
    ads_to_check: Set[str] = set([r["availability_domain"] for r in all_rows if r.get("availability_domain")]) or set(availability_domains)
    shapes_cache_by_ad: Dict[str, Set[str]] = {}
    for ad in sorted([a for a in ads_to_check if a]):
        shapes_cache_by_ad[ad] = list_shapes_in_ad(compute_client, tenancy_id, ad)

    service_name = discover_compute_service_name(limits_client, tenancy_id)
    all_limit_names = list_limit_names(limits_client, tenancy_id, service_name)

    targets = [E5_TARGET, E6_TARGET, STD3_TARGET, OPT3_TARGET]
    target_to_limits: Dict[str, List[str]] = {t: find_limit_names_for_target(all_limit_names, t) for t in targets}
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]] = {}

    # Compute availability + deltas per instance
    for r in all_rows:
        ad = r.get("availability_domain")
        comp_id = r.get("compartment_id", "")
        ocpu = r.get("ocpus")
        mem_gb = r.get("mem_gb")
        cur_cost = r.get("current_cost")

        if r.get("category") == "AMD":
            e5_ok = evaluate_upgrade_option(E5_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            e6_ok = evaluate_upgrade_option(E6_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            r["e5_icon"] = "✅" if e5_ok else "❌"
            r["e6_icon"] = "✅" if e6_ok else "❌"

            # Combine delta since E5 == E6 pricing baseline
            e5_cost = target_monthly_cost(E5_TARGET, ocpu, mem_gb)
            # (E6 cost same, so one delta)
            r["e56_delta"] = round(e5_cost - cur_cost, 2) if (e5_cost is not None and cur_cost is not None) else None

        if r.get("category") == "Intel":
            std3_ok = evaluate_upgrade_option(STD3_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            opt3_ok = evaluate_upgrade_option(OPT3_TARGET, ad, comp_id, shapes_cache_by_ad, target_to_limits, limits_client, service_name, ra_cache)
            r["std3_icon"] = "✅" if std3_ok else "❌"
            r["opt3_icon"] = "✅" if opt3_ok else "❌"

            std3_cost = target_monthly_cost(STD3_TARGET, ocpu, mem_gb)
            opt3_cost = target_monthly_cost(OPT3_TARGET, ocpu, mem_gb)

            std3_delta = (round(std3_cost - cur_cost, 2) if (std3_cost is not None and cur_cost is not None) else None)
            opt3_delta = (round(opt3_cost - cur_cost, 2) if (opt3_cost is not None and cur_cost is not None) else None)

            # "best" = lower delta add-on (most cost-efficient upgrade)
            candidates = [d for d in [std3_delta, opt3_delta] if d is not None]
            r["best_intel_delta"] = min(candidates) if candidates else None

    # Split + sort for HTML (Shape descending)
    amd_rows = sorted([r for r in all_rows if r.get("category") == "AMD"], key=lambda x: x.get("shape", ""), reverse=True)
    intel_rows = sorted([r for r in all_rows if r.get("category") == "Intel"], key=lambda x: x.get("shape", ""), reverse=True)

    # --------------- Generate CSV (keeps OCID + compartment details) ----------------
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "Category", "Risk", "oCPU", "MemoryGB", "Shape", "InstanceName", "Creator", "Lifecycle", "CurrentCostMo",
            f"{E5_TARGET}_avail", f"{E6_TARGET}_avail", "E5_E6_delta_addon",
            f"{STD3_TARGET}_avail", f"{OPT3_TARGET}_avail", "Intel_best_delta_addon",
            "AvailabilityDomain", "CompartmentName", "CompartmentId", "OCID",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for r in all_rows:
            comp_id = r.get("compartment_id", "")
            writer.writerow({
                "Category": r.get("category", ""),
                "Risk": r.get("risk", ""),
                "oCPU": fmt_num(r.get("ocpus")),
                "MemoryGB": fmt_num(r.get("mem_gb")),
                "Shape": r.get("shape", ""),
                "InstanceName": r.get("name", ""),
                "Creator": r.get("creator", "Unknown"),
                "Lifecycle": r.get("lifecycle_state", ""),
                "CurrentCostMo": fmt_money(r.get("current_cost")),
                f"{E5_TARGET}_avail": r.get("e5_icon", ""),
                f"{E6_TARGET}_avail": r.get("e6_icon", ""),
                "E5_E6_delta_addon": fmt_delta(r.get("e56_delta")) if r.get("category") == "AMD" else "",
                f"{STD3_TARGET}_avail": r.get("std3_icon", ""),
                f"{OPT3_TARGET}_avail": r.get("opt3_icon", ""),
                "Intel_best_delta_addon": fmt_delta(r.get("best_intel_delta")) if r.get("category") == "Intel" else "",
                "AvailabilityDomain": r.get("availability_domain", ""),
                "CompartmentName": comp_name_by_id.get(comp_id, ""),
                "CompartmentId": comp_id,
                "OCID": r.get("ocid", ""),
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
    .subtle {{ color:#555; font-size:12px; }}

    .cardwrap {{ display:flex; gap:14px; flex-wrap:wrap; margin-top: 14px; }}
    .card {{ background:#fff; border:1px solid #ddd; border-radius:10px; padding:12px 14px; min-width:240px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
    .card h3 {{ margin:0 0 6px 0; font-size:14px; color:#2c3e50; }}
    .card .big {{ font-size:18px; font-weight:700; }}

    table {{ width:100%; border-collapse:collapse; margin-top:14px; }}
    th, td {{ padding:10px; border:1px solid #ddd; text-align:left; font-size:13px; vertical-align: top; }}
    th {{ background:#34495e; color:white; }}
    tr:nth-child(even) {{ background:#f2f2f2; }}

    .critrow {{ background:#f8d7da !important; }}  /* deeper red */
    .highrow {{ background:#fdecea !important; }}  /* light red */
    .medrow  {{ background:#fff4e5 !important; }}  /* light orange */

    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size:12px; }}
    .note {{ font-size:12px; color:#555; margin-top:10px; }}
    .num {{ text-align:right; }}
    .center {{ text-align:center; }}
  </style>
</head>
<body>
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p><strong>Generated:</strong> {esc(generated)}</p>
  <p><strong>Old instances found:</strong> {len(all_rows)}</p>
  <p class="subtle">
    Pricing baseline is based on <strong>Jan-2026 OCI pricing list</strong> (OCI Calculator-style monthly USD estimates). Actual tenancy billing may differ (discounts, credits, region).
  </p>

  <div class="cardwrap">
    <div class="card">
      <h3>AMD old instances</h3>
      <div class="big">E2={amd_counts["E2"]} | E3={amd_counts["E3"]} | E4={amd_counts["E4"]}</div>
    </div>
    <div class="card">
      <h3>Intel old instances</h3>
      <div class="big">Standard2={intel_counts["Standard2"]}</div>
    </div>
  </div>

  {html_table_amd(amd_rows) if amd_rows else "<h2>AMD old instances</h2><p class='note'>No AMD old instances found.</p>"}
  {html_table_intel(intel_rows) if intel_rows else "<h2>Intel old instances</h2><p class='note'>No Intel old instances found.</p>"}

  <p class="note">
    ✅/❌ availability is best-effort (shape catalog + quota signals). Always validate capacity with the application owner before changing shapes.
  </p>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️ HTML report saved to: {html_path}")
    print(f"✅ Done. Output prefix: {base_name}")


if __name__ == "__main__":
    main()
