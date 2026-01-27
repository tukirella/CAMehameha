#!/usr/bin/env python3
"""
👑 KING KAI — OCI Shapes Upgrade Report (Costs + E5/E6 deltas)

Run:
  python3 oci_forgotten_resources_king_kai.py --shapes-upgrade-report

Outputs:
  - forgotten_resources_report.html
  - forgotten_resources_report.csv

What it does:
- Scans ALL compartments (root + active sub-compartments)
- Finds instances that match the predefined "old shapes" list:
    AMD:
      VM.Standard.E2.{1,2,4,8}
      VM.Standard.E3.Flex
      VM.Standard.E4.Flex
    Intel:
      VM.Standard2.{1,2,4,8,16,24}
- HTML Table columns (left→right):
    Risk, Shape, Instance Name, Lifecycle, Compartment, OCID,
    Current Cost/mo,
    VM.Standard.E5.Flex (✅/❌), E5 Upgrade Cost (Δ $/mo),
    VM.Standard.E6.Flex (✅/❌), E6 Upgrade Cost (Δ $/mo)

Costs:
- Current cost/mo is pulled from OCI Usage API (COST query) grouped by resourceId for current month.
- Usage API is called in the tenancy HOME REGION (best practice), even if you run Cloud Shell in Frankfurt.
- If Usage API fails (permissions/region), the script prints the real error and shows "Unknown".

Upgrade monthly cost estimate (for delta):
- Uses LIST PAYG unit rates:
    E5/E6: $0.03 per OCPU-hour, $0.002 per GB-hour
  (730 hours per month)
- Delta = estimated_target - current_actual_month_cost

Upgrade availability ✅/❌:
- ✅ if target shape exists in AD catalog (list_shapes) AND quota/available signals are not explicitly 0 (when supported).
- ❌ if not offered in AD, or quota/available is 0, or AD is unknown.

Notes:
- E5/E6 columns apply to AMD old shapes. Intel old shapes show "—" in E5/E6 columns.
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

# Upgrade targets (advisor-level mapping)
AMD_UPGRADE_TARGETS = ["VM.Standard.E5.Flex", "VM.Standard.E6.Flex"]
INTEL_UPGRADE_TARGETS = ["VM.Standard3.Flex", "VM.Optimized3.Flex"]  # kept for advisor context

# For the TABLE request in this phase: only E5/E6 columns are shown
E5_TARGET = "VM.Standard.E5.Flex"
E6_TARGET = "VM.Standard.E6.Flex"

# Pricing (list PAYG). Used to estimate E5/E6 monthly cost.
# E5/E6: $0.03 per OCPU-hour, $0.002 per GB-hour
E5_E6_OCPU_PER_HOUR_USD = 0.03
E5_E6_MEM_GB_PER_HOUR_USD = 0.002
HOURS_PER_MONTH = 730  # monthly estimation baseline


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def esc(s: Any) -> str:
    if s is None:
        return ""
    return htmlmod.escape(str(s), quote=True)


def fmt_money(v: Optional[float], currency_symbol: str = "$") -> str:
    if v is None:
        return "Unknown"
    try:
        return f"{currency_symbol}{v:,.2f}"
    except Exception:
        return "Unknown"


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


def get_home_region_name(identity_client, tenancy_id: str) -> str:
    """
    Returns the tenancy home region name (e.g., eu-frankfurt-1) by mapping home_region_key to region name.
    If anything fails, returns empty string.
    """
    try:
        tenancy = identity_client.get_tenancy(tenancy_id).data
        home_key = getattr(tenancy, "home_region_key", None)
        if not home_key:
            return ""

        regions = oci.pagination.list_call_get_all_results(identity_client.list_regions).data
        for r in regions:
            if getattr(r, "key", None) == home_key:
                return getattr(r, "name", "")
    except Exception:
        pass
    return ""


# ------------------------------------------------------------
#  Shape -> Risk and shape -> upgrade targets
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
      Standard2.* -> Standard3.Flex / Optimized3.Flex (not shown in table in this phase)
    """
    if re.match(r"^VM\.Standard\.E2\.\d+$", shape) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex"):
        return [E5_TARGET, E6_TARGET]
    if re.match(r"^VM\.Standard2\.\d+$", shape):
        return INTEL_UPGRADE_TARGETS
    return []


# ------------------------------------------------------------
#  OCPU/Memory inference for legacy fixed shapes
# ------------------------------------------------------------
E2_FIXED_MEM_GB = {
    "VM.Standard.E2.1": 8,
    "VM.Standard.E2.2": 16,
    "VM.Standard.E2.4": 32,
    "VM.Standard.E2.8": 64,
}

STD2_FIXED_MEM_GB = {
    "VM.Standard2.1": 15,
    "VM.Standard2.2": 30,
    "VM.Standard2.4": 60,
    "VM.Standard2.8": 120,
    "VM.Standard2.16": 240,
    # Best-effort for 24:
    "VM.Standard2.24": 360,
}


def infer_ocpu_mem(shape: str, shape_config: Optional[Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (ocpus, memory_gb).
    - For Flex shapes: use shape_config.ocpus and shape_config.memory_in_gbs when available
    - For fixed E2/Standard2: infer from known mappings
    """
    if shape_config is not None:
        ocpus = getattr(shape_config, "ocpus", None)
        mem = getattr(shape_config, "memory_in_gbs", None)
        if ocpus is not None and mem is not None:
            return float(ocpus), float(mem)

    if shape in E2_FIXED_MEM_GB:
        try:
            ocpu = float(shape.split(".")[-1])
        except Exception:
            ocpu = None
        mem = float(E2_FIXED_MEM_GB.get(shape))
        return ocpu, mem

    if shape in STD2_FIXED_MEM_GB:
        try:
            ocpu = float(shape.split(".")[-1])
        except Exception:
            ocpu = None
        mem = float(STD2_FIXED_MEM_GB.get(shape))
        return ocpu, mem

    return None, None


def estimate_e5_e6_monthly_cost_usd(ocpus: Optional[float], mem_gb: Optional[float]) -> Optional[float]:
    if ocpus is None or mem_gb is None:
        return None
    hourly = (ocpus * E5_E6_OCPU_PER_HOUR_USD) + (mem_gb * E5_E6_MEM_GB_PER_HOUR_USD)
    return round(hourly * HOURS_PER_MONTH, 2)


# ------------------------------------------------------------
#  Limits helpers (console-style signals)
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


def build_limit_value_index(limits_client, compartment_id: str, service_name: str) -> Dict[Tuple[str, str, Optional[str]], Any]:
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
#  Usage API (current month cost by resourceId)
# ------------------------------------------------------------
def utc_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_current_month_window_utc() -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    end = now
    return start, end


def fetch_month_costs_by_resource_id(
    usage_client,
    tenancy_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> Tuple[Dict[str, float], str]:
    """
    Best-effort: returns (resource_id -> computed_amount, currency_symbol)
    If API fails, returns ({}, "$") and prints the real error.
    """
    costs: Dict[str, float] = {}
    currency_symbol = "$"

    try:
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=tenancy_id,
            time_usage_started=utc_rfc3339(start_utc),
            time_usage_ended=utc_rfc3339(end_utc),
            granularity="MONTHLY",
            query_type="COST",
            group_by=["resourceId"],
        )

        call = getattr(usage_client, "request_summarized_usages", None) or getattr(usage_client, "request_summarized_usage", None)
        if call is None:
            print("⚠️ Usage API client missing request_summarized_usages method; cannot query costs.")
            return {}, currency_symbol

        resp = call(details)
        data = resp.data

        currency = getattr(data, "currency", None) or getattr(data, "billing_currency", None)
        if isinstance(currency, str) and currency.upper() == "USD":
            currency_symbol = "$"

        items = getattr(data, "items", []) or []
        for it in items:
            dims = getattr(it, "dimensions", {}) or {}
            rid = dims.get("resourceId") or dims.get("resource_id")
            if not rid:
                continue

            amt = (
                getattr(it, "computed_amount_in_billing_currency", None)
                if getattr(it, "computed_amount_in_billing_currency", None) is not None
                else getattr(it, "computed_amount", None)
            )
            if amt is None:
                continue

            try:
                costs[rid] = float(amt)
            except Exception:
                continue

        return costs, currency_symbol

    except Exception as e:
        print(f"⚠️ Usage API cost query failed: {e}")
        return {}, currency_symbol


# ------------------------------------------------------------
#  Scan: shapes-only
# ------------------------------------------------------------
def scan_compartment_shapes_only(
    comp_id: str,
    compute_client,
    old_shapes_set: Optional[Set[str]],
    old_shape_regex: Optional[re.Pattern],
    old_gen_tracker: List[Dict[str, Any]],
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

        # Enrich with get_instance to reliably get AD + shape_config
        ad = getattr(inst, "availability_domain", None)
        shape_config = getattr(inst, "shape_config", None)
        lifecycle = getattr(inst, "lifecycle_state", None)
        name = getattr(inst, "display_name", inst.id)
        ocid = getattr(inst, "id", None)

        try:
            full = compute_client.get_instance(inst.id).data
            ad = getattr(full, "availability_domain", ad)
            shape_config = getattr(full, "shape_config", shape_config)
            lifecycle = getattr(full, "lifecycle_state", lifecycle)
            name = getattr(full, "display_name", name)
        except Exception:
            pass

        old_gen_tracker.append({
            "name": name,
            "shape": shape,
            "availability_domain": ad,
            "compartment_id": comp_id,
            "lifecycle_state": lifecycle,
            "ocid": ocid,
            "shape_config": shape_config,
        })


# ------------------------------------------------------------
#  Advisor summary block (kept)
# ------------------------------------------------------------
def compute_upgrade_advice_summary(
    old_gen_tracker: List[Dict[str, Any]],
    compute_client,
    limits_client,
    tenancy_id: str,
    availability_domains: List[str],
) -> Dict[str, Any]:
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

    for k in ["AMD_E2", "AMD_E3", "AMD_E4", "INTEL_STD2"]:
        counts.setdefault(k, 0)

    targets: List[str] = []
    if (counts["AMD_E2"] + counts["AMD_E3"] + counts["AMD_E4"]) > 0:
        targets.extend(AMD_UPGRADE_TARGETS)
    if counts["INTEL_STD2"] > 0:
        targets.extend(INTEL_UPGRADE_TARGETS)

    advice: Dict[str, Any] = {
        "counts": dict(counts),
        "targets": targets,
        "ads": sorted(list(ads_seen)) if ads_seen else sorted([a for a in availability_domains if a]),
        "catalog": {},
        "compute_service": "",
        "target_to_limits": {},
    }

    if not targets:
        return advice

    # Catalog per AD (for advisor header only)
    for ad in advice["ads"]:
        shape_set = list_shapes_in_ad(compute_client, tenancy_id, ad)
        advice["catalog"][ad] = {
            "targets": {t: (t in shape_set) for t in targets},
        }

    service_name = discover_compute_service_name(limits_client, tenancy_id)
    advice["compute_service"] = service_name

    limit_index = build_limit_value_index(limits_client, tenancy_id, service_name)
    all_limit_names = {k[0] for k in limit_index.keys() if k and k[0]}

    target_to_limits: Dict[str, List[str]] = {}
    for t in targets:
        target_to_limits[t] = find_limit_names_for_target(all_limit_names, t)
    advice["target_to_limits"] = target_to_limits

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
#  HTML rendering (Advisor header)
# ------------------------------------------------------------
def render_advisor_block_html(advice: Dict[str, Any]) -> str:
    if not advice:
        return ""

    c = advice.get("counts", {}) or {}
    amd_line = f"E2={c.get('AMD_E2',0)} | E3={c.get('AMD_E3',0)} | E4={c.get('AMD_E4',0)}"
    intel_line = f"Standard2={c.get('INTEL_STD2',0)}"
    targets: List[str] = advice.get("targets", []) or []
    ads: List[str] = advice.get("ads", []) or []

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

    if ads and targets:
        out.append('<h3>Shape catalog availability (per AD)</h3>')
        out.append('<table class="advisor-table">')
        out.append('<tr><th>Availability Domain</th>' + "".join([f"<th>{esc(t)}</th>" for t in targets]) + '</tr>')
        for ad in ads:
            entry = (advice.get("catalog", {}) or {}).get(ad, {}) or {}
            tgt_map = entry.get("targets", {}) or {}
            row = [f"<td>{esc(ad)}</td>"]
            for t in targets:
                row.append(f"<td>{'✅' if tgt_map.get(t, False) else '❌'}</td>")
            out.append("<tr>" + "".join(row) + "</tr>")
        out.append("</table>")

    out.append("</div>")
    return "\n".join(out)


# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — Shapes Upgrade Report (Costs + E5/E6 deltas)")

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

    # Usage API client (best-effort for current monthly spend) -> use HOME REGION
    usage_client = None
    usage_region = get_home_region_name(identity_client, tenancy_id)
    try:
        usage_cfg = dict(config)
        if usage_region:
            usage_cfg["region"] = usage_region
        usage_client = oci.usage_api.UsageapiClient(usage_cfg)
    except Exception as e:
        print(f"⚠️ Usage API client init failed (home_region={usage_region or 'unknown'}): {e}")
        usage_client = None

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

    old_gen_tracker: List[Dict[str, Any]] = []

    for comp_id in compartments:
        scan_compartment_shapes_only(
            comp_id=comp_id,
            compute_client=compute_client,
            old_shapes_set=old_shapes_set,
            old_shape_regex=old_shape_regex,
            old_gen_tracker=old_gen_tracker,
        )

    # Advisor summary
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

    # Current month costs by resource id
    start_utc, end_utc = get_current_month_window_utc()
    costs_by_rid: Dict[str, float] = {}
    currency_symbol = "$"
    if usage_client is not None:
        costs_by_rid, currency_symbol = fetch_month_costs_by_resource_id(
            usage_client=usage_client,
            tenancy_id=tenancy_id,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    # --------------- Generate CSV ----------------
    csv_path = args.output_csv
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "Risk", "Shape", "InstanceName", "Lifecycle", "CompartmentId", "OCID",
            "CurrentCostMo",
            "E5_Available", "E5_DeltaMo",
            "E6_Available", "E6_DeltaMo",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for row in old_gen_tracker:
            shape = row.get("shape", "") or ""
            risk = risk_for_shape(shape)
            ocid = row.get("ocid", "")

            current_cost = costs_by_rid.get(ocid)
            shape_config = row.get("shape_config")
            ocpus, mem_gb = infer_ocpu_mem(shape, shape_config)

            # Defaults for non-AMD
            e5_icon = "—"
            e6_icon = "—"
            e5_delta = None
            e6_delta = None

            # AMD old shapes => evaluate availability + compute deltas
            if re.match(r"^VM\.Standard\.E2\.\d+$", shape) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex"):
                e5_ok = evaluate_upgrade_option(
                    target_shape=E5_TARGET,
                    instance_ad=row.get("availability_domain"),
                    instance_compartment_id=row.get("compartment_id", ""),
                    shapes_cache_by_ad=shapes_cache_by_ad,
                    target_to_limits=target_to_limits,
                    limits_client=limits_client,
                    service_name=service_name,
                    ra_cache=ra_cache,
                )
                e6_ok = evaluate_upgrade_option(
                    target_shape=E6_TARGET,
                    instance_ad=row.get("availability_domain"),
                    instance_compartment_id=row.get("compartment_id", ""),
                    shapes_cache_by_ad=shapes_cache_by_ad,
                    target_to_limits=target_to_limits,
                    limits_client=limits_client,
                    service_name=service_name,
                    ra_cache=ra_cache,
                )
                e5_icon = "✅" if e5_ok else "❌"
                e6_icon = "✅" if e6_ok else "❌"

                # delta only (requires current month cost)
                if current_cost is not None:
                    e5_est = estimate_e5_e6_monthly_cost_usd(ocpus, mem_gb)
                    e6_est = estimate_e5_e6_monthly_cost_usd(ocpus, mem_gb)
                    if e5_est is not None:
                        e5_delta = round(e5_est - float(current_cost), 2)
                    if e6_est is not None:
                        e6_delta = round(e6_est - float(current_cost), 2)

            writer.writerow({
                "Risk": risk,
                "Shape": shape,
                "InstanceName": row.get("name", ""),
                "Lifecycle": row.get("lifecycle_state", ""),
                "CompartmentId": row.get("compartment_id", ""),
                "OCID": ocid,
                "CurrentCostMo": fmt_money(current_cost, currency_symbol),
                "E5_Available": e5_icon,
                "E5_DeltaMo": fmt_money(e5_delta, currency_symbol) if e5_delta is not None else ("—" if e5_icon == "—" else "Unknown"),
                "E6_Available": e6_icon,
                "E6_DeltaMo": fmt_money(e6_delta, currency_symbol) if e6_delta is not None else ("—" if e6_icon == "—" else "Unknown"),
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
  </style>
</head>
<body>
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p><strong>Generated:</strong> {esc(now)}</p>
  <p><strong>Old instances found:</strong> {len(old_gen_tracker)}</p>
  <p class="note">
    Usage API region (home region): <strong>{esc(usage_region or "unknown")}</strong><br/>
    Current cost/mo is pulled from OCI Usage API for the current month window ({esc(start_utc.strftime("%Y-%m-%d"))} → {esc(end_utc.strftime("%Y-%m-%d"))}).<br/>
    E5/E6 deltas use list PAYG estimation (OCPU+Memory) with {HOURS_PER_MONTH} hrs/month.
  </p>

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
      <th>Current Cost/mo</th>
      <th>{esc(E5_TARGET)}</th>
      <th>E5 Upgrade Cost (Δ/mo)</th>
      <th>{esc(E6_TARGET)}</th>
      <th>E6 Upgrade Cost (Δ/mo)</th>
    </tr>
"""

    for row in old_gen_tracker:
        shape = row.get("shape", "") or ""
        risk = risk_for_shape(shape)
        row_class = "highrow" if risk == "High" else "medrow"

        comp_id = row.get("compartment_id", "")
        comp_name = comp_name_by_id.get(comp_id, "")
        comp_cell = f"{esc(comp_name)}<br/><span class='mono'>{esc(comp_id)}</span>" if comp_name else f"<span class='mono'>{esc(comp_id)}</span>"

        ocid = row.get("ocid", "")
        current_cost = costs_by_rid.get(ocid)

        shape_config = row.get("shape_config")
        ocpus, mem_gb = infer_ocpu_mem(shape, shape_config)

        # Defaults for non-AMD
        e5_icon = "—"
        e6_icon = "—"
        e5_delta_str = "—"
        e6_delta_str = "—"

        # AMD old shapes => evaluate + compute deltas
        if re.match(r"^VM\.Standard\.E2\.\d+$", shape) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex"):
            e5_ok = evaluate_upgrade_option(
                target_shape=E5_TARGET,
                instance_ad=row.get("availability_domain"),
                instance_compartment_id=comp_id,
                shapes_cache_by_ad=shapes_cache_by_ad,
                target_to_limits=target_to_limits,
                limits_client=limits_client,
                service_name=service_name,
                ra_cache=ra_cache,
            )
            e6_ok = evaluate_upgrade_option(
                target_shape=E6_TARGET,
                instance_ad=row.get("availability_domain"),
                instance_compartment_id=comp_id,
                shapes_cache_by_ad=shapes_cache_by_ad,
                target_to_limits=target_to_limits,
                limits_client=limits_client,
                service_name=service_name,
                ra_cache=ra_cache,
            )

            e5_icon = "✅" if e5_ok else "❌"
            e6_icon = "✅" if e6_ok else "❌"

            if current_cost is not None:
                e5_est = estimate_e5_e6_monthly_cost_usd(ocpus, mem_gb)
                e6_est = estimate_e5_e6_monthly_cost_usd(ocpus, mem_gb)
                e5_delta_str = fmt_money(round(e5_est - float(current_cost), 2), currency_symbol) if e5_est is not None else "Unknown"
                e6_delta_str = fmt_money(round(e6_est - float(current_cost), 2), currency_symbol) if e6_est is not None else "Unknown"
            else:
                e5_delta_str = "Unknown"
                e6_delta_str = "Unknown"

        html_content += f"""
    <tr class="{row_class}">
      <td><strong>{esc(risk)}</strong></td>
      <td class="mono">{esc(shape)}</td>
      <td>{esc(row.get('name',''))}</td>
      <td>{esc(row.get('lifecycle_state',''))}</td>
      <td>{comp_cell}</td>
      <td class="mono">{esc(ocid)}</td>
      <td>{esc(fmt_money(current_cost, currency_symbol))}</td>
      <td>{esc(e5_icon)}</td>
      <td>{esc(e5_delta_str)}</td>
      <td>{esc(e6_icon)}</td>
      <td>{esc(e6_delta_str)}</td>
    </tr>
"""

    html_content += """
  </table>

  <p class="note">
    ✅/❌ checks AD catalog + quota/availability signals (when available). If the region/AD doesn’t offer E6 (e.g., MTZ/Jerusalem), it will show ❌.
    Upgrade deltas are estimates based on list PAYG compute pricing for E5/E6 with the same OCPU + Memory sizing as the current instance.
  </p>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️  HTML report saved to: {html_path}")


if __name__ == "__main__":
    main()
