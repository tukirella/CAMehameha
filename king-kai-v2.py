#!/usr/bin/env python3
"""
👑 KING KAI — Shapes Upgrade Report

Run:
  python3 king-kai.py

Outputs (UTC timestamp):
  - king-kaiYYYYMMDD-HHMM.html
  - king-kaiYYYYMMDD-HHMM.csv

Key features:
- Scans ALL compartments (including root) in the current region.
- Detects legacy shapes (AMD E2/E3/E4 + Intel Standard2) and recommends upgrade targets.
- Produces 2 HTML tables (AMD + Intel) and a full CSV with OCIDs + compartment details.
- Shows upgrade availability (✅/❌ in HTML, Y/N in CSV).
- Adds "Creator" based on Oracle-Tags.CreatedBy (strips "default/" for human users).
- Adds "30 days uptime (h)" based on Monitoring metric:
    namespace: oci_compute_infrastructure_health
    metric: instance_status
  (When stopped, metric has no value → we count datapoints windows as uptime.)

Notes:
- Pricing stays as monthly baseline (your Jan-2026 calculator table).
- Uptime is NOT used to prorate costs yet (as requested).
"""

import oci
import csv
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any, Set

# -----------------------------
# Shapes to detect
# -----------------------------
OLD_SHAPES_SET: Set[str] = {
    # AMD E2 (fixed names)
    "VM.Standard.E2.1",
    "VM.Standard.E2.2",
    "VM.Standard.E2.4",
    "VM.Standard.E2.8",
    # AMD E3/E4 flex
    "VM.Standard.E3.Flex",
    "VM.Standard.E4.Flex",
    # Intel Standard2 (fixed)
    "VM.Standard2.1",
    "VM.Standard2.2",
    "VM.Standard2.4",
    "VM.Standard2.8",
    "VM.Standard2.16",
    "VM.Standard2.24",
}

# Upgrade targets
E5_TARGET = "VM.Standard.E5.Flex"
E6_TARGET = "VM.Standard.E6.Flex"
STD3_TARGET = "VM.Standard3.Flex"
OPT3_TARGET = "VM.Optimized3.Flex"

# Risk order (ascending): Critical first
RISK_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Unknown": 9}

# -----------------------------
# Pricing baseline (Monthly USD) — from your OCI Calculator table (Jan-2026)
# -----------------------------
FLEX_BASE_OCPU = 1.0
FLEX_BASE_MEM_GB = 8.0

AMD_FLEX_PRICING = {
    "E2": {"base": 27.0, "extra_ocpu": 14.5, "extra_gb": 1.0},
    "E3": {"base": 27.0, "extra_ocpu": 18.0, "extra_gb": 1.0},
    "E4": {"base": 27.0, "extra_ocpu": 20.0, "extra_gb": 1.0},
    "E5": {"base": 34.0, "extra_ocpu": 22.5, "extra_gb": 1.5},
    "E6": {"base": 34.0, "extra_ocpu": 22.5, "extra_gb": 1.5},
}

INTEL_STD2_FIXED = {
    "VM.Standard2.1":  {"ocpu": 1,  "mem": 8,   "cost": 47.50},
    "VM.Standard2.2":  {"ocpu": 2,  "mem": 30,  "cost": 95.00},
    "VM.Standard2.4":  {"ocpu": 4,  "mem": 60,  "cost": 190.00},
    "VM.Standard2.8":  {"ocpu": 8,  "mem": 120, "cost": 380.00},
    "VM.Standard2.16": {"ocpu": 16, "mem": 240, "cost": 760.00},
    "VM.Standard2.24": {"ocpu": 24, "mem": 320, "cost": 1140.00},
}

INTEL_FLEX_PRICING = {
    "STD3": {"base": 39.0, "extra_ocpu": 30.0, "extra_gb": 1.1},
    "OPT3": {"base": 49.0, "extra_ocpu": 40.0, "extra_gb": 1.1},
}

# -----------------------------
# Uptime calculation settings
# -----------------------------
UPTIME_DAYS = 30
UPTIME_INTERVAL_MIN = 5          # 5-minute resolution
UPTIME_INTERVAL_STR = "5m"       # MQL interval


def utc_now_minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def get_creator_from_defined_tags(defined_tags: Optional[Dict[str, Any]]) -> str:
    """
    Prefer Oracle-Tags.CreatedBy if exists.
    Strip "default/" for human users.
    Keep full syntax for service principals / OCIDs.
    """
    if not defined_tags or not isinstance(defined_tags, dict):
        return "Unknown"

    creator = None
    oracle_tags = defined_tags.get("Oracle-Tags") or defined_tags.get("oracle-tags")
    if isinstance(oracle_tags, dict):
        creator = oracle_tags.get("CreatedBy") or oracle_tags.get("createdBy")

    if not creator or not isinstance(creator, str) or not creator.strip():
        return "Unknown"

    creator = creator.strip()

    # Strip "default/" for typical human identities, but keep full for OCIDs/services
    if creator.startswith("default/") and ("ocid1." not in creator) and (":" not in creator):
        return creator[len("default/"):].strip()

    return creator


def collect_all_compartments(identity_client, tenancy_id: str) -> List[Tuple[str, str]]:
    """
    Returns list of (compartment_ocid, compartment_name) including root.
    """
    compartments: List[Tuple[str, str]] = []
    all_response = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        compartment_id=tenancy_id,
        compartment_id_in_subtree=True,
        lifecycle_state="ACTIVE"
    )
    for cp in all_response.data:
        compartments.append((cp.id, cp.name))
    compartments.append((tenancy_id, "ROOT"))
    return compartments


def is_amd_shape(shape: str) -> bool:
    return shape.startswith("VM.Standard.E2") or shape.startswith("VM.Standard.E3") or shape.startswith("VM.Standard.E4")


def is_intel_std2(shape: str) -> bool:
    return shape.startswith("VM.Standard2.")


def risk_for_shape(shape: str) -> str:
    # Requested:
    # - Critical for VM.Standard.E2.1
    # - High for AMD E2.* and AMD E3.Flex
    # - Medium for the rest (in our legacy scan set)
    if shape == "VM.Standard.E2.1":
        return "Critical"
    if shape.startswith("VM.Standard.E2.") or shape == "VM.Standard.E3.Flex":
        return "High"
    return "Medium"


def get_ocpu_mem(instance) -> Tuple[Optional[float], Optional[float]]:
    """
    For FLEX shapes, derive from instance.shape_config.
    For fixed Intel Standard2, use fixed table.
    """
    shape = getattr(instance, "shape", "") or ""
    if shape in INTEL_STD2_FIXED:
        return float(INTEL_STD2_FIXED[shape]["ocpu"]), float(INTEL_STD2_FIXED[shape]["mem"])

    sc = getattr(instance, "shape_config", None)
    if sc:
        ocpu = safe_float(getattr(sc, "ocpus", None))
        mem = safe_float(getattr(sc, "memory_in_gbs", None))
        return ocpu, mem

    return None, None


def calc_flex_cost(series: str, ocpu: float, mem: float, pricing: Dict[str, Dict[str, float]]) -> float:
    p = pricing[series]
    extra_ocpu = max(0.0, ocpu - FLEX_BASE_OCPU)
    extra_mem = max(0.0, mem - FLEX_BASE_MEM_GB)
    return round(p["base"] + (extra_ocpu * p["extra_ocpu"]) + (extra_mem * p["extra_gb"]), 2)


def current_cost_monthly(shape: str, ocpu: Optional[float], mem: Optional[float]) -> Optional[float]:
    if not shape:
        return None

    # Intel Standard2 fixed
    if shape in INTEL_STD2_FIXED:
        return float(INTEL_STD2_FIXED[shape]["cost"])

    # AMD E2/E3/E4 flex-style pricing (per your table)
    if ocpu is None or mem is None:
        return None

    if shape.startswith("VM.Standard.E2"):
        return calc_flex_cost("E2", ocpu, mem, AMD_FLEX_PRICING)
    if shape.startswith("VM.Standard.E3"):
        return calc_flex_cost("E3", ocpu, mem, AMD_FLEX_PRICING)
    if shape.startswith("VM.Standard.E4"):
        return calc_flex_cost("E4", ocpu, mem, AMD_FLEX_PRICING)

    return None


def e5e6_monthly_addon(shape: str, ocpu: Optional[float], mem: Optional[float]) -> Optional[float]:
    """
    Delta add-on monthly if upgraded to E5/E6 Flex (same pricing per your note).
    """
    if ocpu is None or mem is None:
        return None
    if not is_amd_shape(shape):
        return None
    cur = current_cost_monthly(shape, ocpu, mem)
    if cur is None:
        return None
    tgt = calc_flex_cost("E5", ocpu, mem, AMD_FLEX_PRICING)  # E5/E6 same in your table
    return round(tgt - cur, 2)


def std3_monthly_addon(shape: str, ocpu: Optional[float], mem: Optional[float]) -> Optional[float]:
    if not is_intel_std2(shape) or ocpu is None or mem is None:
        return None
    cur = current_cost_monthly(shape, ocpu, mem)
    if cur is None:
        return None
    tgt = calc_flex_cost("STD3", ocpu, mem, INTEL_FLEX_PRICING)
    return round(tgt - cur, 2)


def opt3_monthly_addon(shape: str, ocpu: Optional[float], mem: Optional[float]) -> Optional[float]:
    if not is_intel_std2(shape) or ocpu is None or mem is None:
        return None
    cur = current_cost_monthly(shape, ocpu, mem)
    if cur is None:
        return None
    tgt = calc_flex_cost("OPT3", ocpu, mem, INTEL_FLEX_PRICING)
    return round(tgt - cur, 2)


def list_shapes_in_ad_cached(compute_client, tenancy_id: str, availability_domain: str,
                             cache: Dict[str, Set[str]]) -> Set[str]:
    key = availability_domain
    if key in cache:
        return cache[key]

    shapes = oci.pagination.list_call_get_all_results(
        compute_client.list_shapes,
        compartment_id=tenancy_id,
        availability_domain=availability_domain
    ).data

    sset = set([s.shape for s in shapes if getattr(s, "shape", None)])
    cache[key] = sset
    return sset


def yes_no(flag: bool) -> str:
    return "Y" if flag else "N"


def yn_emoji(flag: bool) -> str:
    return "✅" if flag else "❌"


def compute_uptime_30d_hours(
    monitoring_client,
    compartment_id: str,
    instance_ocid: str,
    start_time: datetime,
    end_time: datetime
) -> Optional[float]:
    """
    Uses Monitoring MQL:
      namespace: oci_compute_infrastructure_health
      query: instance_status[5m]{resourceId="..."} .count()

    We count each 5m bucket that has observations (count > 0) as uptime.
    Returns hours (float, 1 decimal). If no access / errors → None.
    """
    try:
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace="oci_compute_infrastructure_health",
            query=f'instance_status[{UPTIME_INTERVAL_STR}]{{resourceId="{instance_ocid}"}}.count()',
            start_time=start_time,
            end_time=end_time,
            resolution=UPTIME_INTERVAL_STR
        )
        resp = monitoring_client.summarize_metrics_data(
            compartment_id=compartment_id,
            summarize_metrics_data_details=details
        )

        total_buckets = 0
        for series in (resp.data or []):
            dps = getattr(series, "aggregated_datapoints", None) or []
            for dp in dps:
                val = getattr(dp, "value", None)
                if val is None:
                    continue
                try:
                    if float(val) > 0:
                        total_buckets += 1
                except Exception:
                    continue

        minutes = total_buckets * UPTIME_INTERVAL_MIN
        hours = round(minutes / 60.0, 1)
        return hours
    except Exception:
        return None


def html_escape(s: Any) -> str:
    return htmlmod.escape(str(s)) if s is not None else ""


def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Risk asc (Critical→High→Medium), then Shape desc, then name asc
    def key(r: Dict[str, Any]):
        risk = r.get("Risk", "Unknown")
        risk_rank = RISK_RANK.get(risk, 9)
        shape = r.get("Shape", "")
        name = r.get("InstanceName", "")
        # shape desc → use negative via reverse later; easiest: return and sort with reverse on shape
        return (risk_rank, shape, name)

    # We'll do stable sort in 2 passes: name asc, shape desc, risk asc
    rows = sorted(rows, key=lambda r: (r.get("InstanceName", "") or ""))
    rows = sorted(rows, key=lambda r: (r.get("Shape", "") or ""), reverse=True)
    rows = sorted(rows, key=lambda r: RISK_RANK.get(r.get("Risk", "Unknown"), 9))
    return rows


def generate_html(filename: str, amd_rows: List[Dict[str, Any]], intel_rows: List[Dict[str, Any]], generated_at: str) -> None:
    def risk_class(risk: str) -> str:
        return {
            "Critical": "critical",
            "High": "high",
            "Medium": "medium",
            "Low": "low",
        }.get(risk, "unknown")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>KING KAI — Shapes Upgrade Report</title>
<style>
  body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f9f9f9; margin:20px; }}
  h1 {{ color:#2c3e50; margin-bottom:6px; }}
  .sub {{ color:#555; margin-top:0; }}
  .box {{ background:#fff; border:1px solid #e6e6e6; border-radius:12px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,0.04); }}
  table {{ width:100%; border-collapse:collapse; margin-top:14px; background:#fff; border:1px solid #e6e6e6; border-radius:12px; overflow:hidden; }}
  th, td {{ padding:10px; border-bottom:1px solid #eee; font-size:13px; text-align:left; }}
  th {{ background:#34495e; color:white; position:sticky; top:0; }}
  tr:nth-child(even) td {{ background:#fafafa; }}
  .critical td {{ background:#ffe7e7 !important; }}
  .high td {{ background:#fdecea !important; }}
  .medium td {{ background:#fff4e5 !important; }}
  .low td {{ background:#e8f5e9 !important; }}
  .unknown td {{ background:#f0f0f0 !important; }}
  .footer {{ margin-top:16px; color:#666; font-size:12px; }}
</style>
</head>
<body>

<div class="box">
  <h1>👑 KING KAI — Shapes Upgrade Report</h1>
  <p class="sub"><strong>Generated:</strong> {generated_at} (UTC)</p>
  <p class="sub">
    Scans all compartments in this region to identify legacy shapes and show upgrade availability.<br/>
    <strong>Pricing baseline:</strong> OCI Calculator snapshot (Jan-2026) — values are indicative and subject to change.
  </p>
</div>

<h2>AMD Instances (E2 / E3 / E4 → E5/E6 Flex)</h2>
<table>
  <tr>
    <th>Risk</th>
    <th>oCPU</th>
    <th>Memory [GB]</th>
    <th>Shape</th>
    <th>Instance Name</th>
    <th>Creator</th>
    <th>Lifecycle</th>
    <th>30 days uptime (h)</th>
    <th>Current Cost/mo</th>
    <th>VM.Standard.E5.Flex avail</th>
    <th>VM.Standard.E6.Flex avail</th>
    <th>E5/E6.Flex monthly add-on</th>
  </tr>
"""

    for r in amd_rows:
        cls = risk_class(r.get("Risk", "Unknown"))
        html += f"""
  <tr class="{cls}">
    <td>{html_escape(r.get("Risk"))}</td>
    <td>{html_escape(r.get("OCPU"))}</td>
    <td>{html_escape(r.get("MemoryGB"))}</td>
    <td>{html_escape(r.get("Shape"))}</td>
    <td>{html_escape(r.get("InstanceName"))}</td>
    <td>{html_escape(r.get("Creator"))}</td>
    <td>{html_escape(r.get("Lifecycle"))}</td>
    <td>{html_escape(r.get("Uptime30dHours"))}</td>
    <td>{html_escape(r.get("CurrentCostMo"))}</td>
    <td>{html_escape(r.get("E5AvailEmoji"))}</td>
    <td>{html_escape(r.get("E6AvailEmoji"))}</td>
    <td>{html_escape(r.get("E5E6MonthlyAddon"))}</td>
  </tr>
"""

    html += """
</table>

<h2>Intel Instances (Standard2 → Standard3.Flex / Optimized3.Flex)</h2>
<table>
  <tr>
    <th>Risk</th>
    <th>oCPU</th>
    <th>Memory [GB]</th>
    <th>Shape</th>
    <th>Instance Name</th>
    <th>Creator</th>
    <th>Lifecycle</th>
    <th>30 days uptime (h)</th>
    <th>Current Cost/mo</th>
    <th>VM.Standard3.Flex avail</th>
    <th>VM.Standard3.Flex monthly add-on</th>
    <th>VM.Optimized3.Flex avail</th>
    <th>VM.Optimized3.Flex monthly add-on</th>
  </tr>
"""
    for r in intel_rows:
        cls = risk_class(r.get("Risk", "Unknown"))
        html += f"""
  <tr class="{cls}">
    <td>{html_escape(r.get("Risk"))}</td>
    <td>{html_escape(r.get("OCPU"))}</td>
    <td>{html_escape(r.get("MemoryGB"))}</td>
    <td>{html_escape(r.get("Shape"))}</td>
    <td>{html_escape(r.get("InstanceName"))}</td>
    <td>{html_escape(r.get("Creator"))}</td>
    <td>{html_escape(r.get("Lifecycle"))}</td>
    <td>{html_escape(r.get("Uptime30dHours"))}</td>
    <td>{html_escape(r.get("CurrentCostMo"))}</td>
    <td>{html_escape(r.get("STD3AvailEmoji"))}</td>
    <td>{html_escape(r.get("STD3MonthlyAddon"))}</td>
    <td>{html_escape(r.get("OPT3AvailEmoji"))}</td>
    <td>{html_escape(r.get("OPT3MonthlyAddon"))}</td>
  </tr>
"""

    html += f"""
</table>

<div class="footer">
  <p><strong>Tip:</strong> 30-days uptime is computed from Monitoring metric <code>oci_compute_infrastructure_health / instance_status</code>.
  When an instance is stopped, this metric has no datapoints (so uptime naturally decreases).</p>
</div>

</body>
</html>
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    # Load config (no mandatory flags)
    try:
        config = oci.config.from_file()
    except Exception as e:
        print(f"❌ Failed to load OCI config from ~/.oci/config: {e}")
        sys.exit(1)

    tenancy_id = config.get("tenancy")
    if not tenancy_id:
        print("❌ Missing 'tenancy' in OCI config.")
        sys.exit(1)

    identity_client = oci.identity.IdentityClient(config)
    compute_client = oci.core.ComputeClient(config)
    monitoring_client = oci.monitoring.MonitoringClient(config)

    now = utc_now_minute()
    start_30d = now - timedelta(days=UPTIME_DAYS)

    stamp = now.strftime("%Y%m%d-%H%M")
    csv_name = f"king-kai{stamp}.csv"
    html_name = f"king-kai{stamp}.html"

    print(f"👑 KING KAI starting… (region={config.get('region')}, UTC={now.isoformat()})")
    print(f"📄 Outputs: {html_name} , {csv_name}")
    print()

    # Collect compartments
    compartments = collect_all_compartments(identity_client, tenancy_id)
    print(f"🔍 Found {len(compartments)} compartments (including root).")
    print()

    # Cache shapes availability per AD
    shapes_cache: Dict[str, Set[str]] = {}

    all_rows: List[Dict[str, Any]] = []

    for idx, (comp_id, comp_name) in enumerate(compartments, start=1):
        print(f"⏳ [{idx}/{len(compartments)}] Scanning compartment: {comp_name} ({comp_id})")

        try:
            instances = oci.pagination.list_call_get_all_results(
                compute_client.list_instances,
                compartment_id=comp_id
            ).data
        except Exception as e:
            print(f"   ⚠️  Failed to list instances in this compartment: {e}")
            continue

        legacy = [i for i in instances if (getattr(i, "shape", "") or "") in OLD_SHAPES_SET]
        if not legacy:
            print("   ✅ No legacy shapes found here.")
            continue

        print(f"   ⚠️ Found {len(legacy)} legacy-shape instances. Computing uptime + availability…")

        for inst in legacy:
            shape = getattr(inst, "shape", "") or ""
            name = getattr(inst, "display_name", "") or ""
            lifecycle = getattr(inst, "lifecycle_state", "") or ""
            instance_id = getattr(inst, "id", "") or ""
            ad = getattr(inst, "availability_domain", "") or ""

            defined_tags = getattr(inst, "defined_tags", None)
            creator = get_creator_from_defined_tags(defined_tags)

            risk = risk_for_shape(shape)

            ocpu, mem = get_ocpu_mem(inst)

            # Current monthly cost baseline
            cur_cost = current_cost_monthly(shape, ocpu, mem)
            cur_cost_str = f"${cur_cost:,.2f}" if cur_cost is not None else "Unknown"

            # Upgrade add-ons
            e5e6_addon = e5e6_monthly_addon(shape, ocpu, mem)
            e5e6_addon_str = f"${e5e6_addon:,.2f}" if e5e6_addon is not None else "Unknown"

            std3_addon = std3_monthly_addon(shape, ocpu, mem)
            std3_addon_str = f"${std3_addon:,.2f}" if std3_addon is not None else "Unknown"

            opt3_addon = opt3_monthly_addon(shape, ocpu, mem)
            opt3_addon_str = f"${opt3_addon:,.2f}" if opt3_addon is not None else "Unknown"

            # Availability check (shape exists in AD)
            shapes_in_ad = set()
            if ad:
                try:
                    shapes_in_ad = list_shapes_in_ad_cached(compute_client, tenancy_id, ad, shapes_cache)
                except Exception:
                    shapes_in_ad = set()

            e5_avail = E5_TARGET in shapes_in_ad
            e6_avail = E6_TARGET in shapes_in_ad
            std3_avail = STD3_TARGET in shapes_in_ad
            opt3_avail = OPT3_TARGET in shapes_in_ad

            # Uptime (last 30 days)
            uptime_h = compute_uptime_30d_hours(monitoring_client, comp_id, instance_id, start_30d, now)
            uptime_str = f"{uptime_h:.1f}" if uptime_h is not None else "Unknown"

            row = {
                # HTML columns
                "Risk": risk,
                "OCPU": f"{ocpu:.1f}" if isinstance(ocpu, float) else (str(ocpu) if ocpu is not None else "Unknown"),
                "MemoryGB": f"{mem:.1f}" if isinstance(mem, float) else (str(mem) if mem is not None else "Unknown"),
                "Shape": shape,
                "InstanceName": name,
                "Creator": creator,
                "Lifecycle": lifecycle,
                "Uptime30dHours": uptime_str,
                "CurrentCostMo": cur_cost_str,

                "E5AvailEmoji": yn_emoji(e5_avail),
                "E6AvailEmoji": yn_emoji(e6_avail),
                "E5E6MonthlyAddon": e5e6_addon_str,

                "STD3AvailEmoji": yn_emoji(std3_avail),
                "STD3MonthlyAddon": std3_addon_str,
                "OPT3AvailEmoji": yn_emoji(opt3_avail),
                "OPT3MonthlyAddon": opt3_addon_str,

                # CSV extra fields
                "CompartmentId": comp_id,
                "CompartmentName": comp_name,
                "InstanceOCID": instance_id,
                "AvailabilityDomain": ad,
                "Region": config.get("region", ""),
                "E5AvailYN": yes_no(e5_avail),
                "E6AvailYN": yes_no(e6_avail),
                "STD3AvailYN": yes_no(std3_avail),
                "OPT3AvailYN": yes_no(opt3_avail),
                "CurrentCostMoRaw": cur_cost if cur_cost is not None else "",
                "E5E6MonthlyAddonRaw": e5e6_addon if e5e6_addon is not None else "",
                "STD3MonthlyAddonRaw": std3_addon if std3_addon is not None else "",
                "OPT3MonthlyAddonRaw": opt3_addon if opt3_addon is not None else "",
                "Uptime30dHoursRaw": uptime_h if uptime_h is not None else "",
                "Manufacturer": "AMD" if is_amd_shape(shape) else ("Intel" if is_intel_std2(shape) else "Unknown"),
            }
            all_rows.append(row)

        print("   ✅ Done.")
        print()

    amd_rows = [r for r in all_rows if r.get("Manufacturer") == "AMD"]
    intel_rows = [r for r in all_rows if r.get("Manufacturer") == "Intel"]

    amd_rows = sort_rows(amd_rows)
    intel_rows = sort_rows(intel_rows)

    # CSV output (keeps OCID + compartment details)
    csv_fields = [
        "Risk", "Manufacturer", "OCPU", "MemoryGB", "Shape",
        "InstanceName", "Creator", "Lifecycle", "Uptime30dHours",
        "CurrentCostMo", "E5AvailYN", "E6AvailYN", "E5E6MonthlyAddon",
        "STD3AvailYN", "STD3MonthlyAddon", "OPT3AvailYN", "OPT3MonthlyAddon",
        "CompartmentName", "CompartmentId", "AvailabilityDomain", "Region", "InstanceOCID"
    ]

    with open(csv_name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for r in (amd_rows + intel_rows):
            # Make sure Y/N columns are present and emoji-only stays in HTML
            out = dict(r)
            out["E5E6MonthlyAddon"] = out.get("E5E6MonthlyAddon", "Unknown")
            out["STD3MonthlyAddon"] = out.get("STD3MonthlyAddon", "Unknown")
            out["OPT3MonthlyAddon"] = out.get("OPT3MonthlyAddon", "Unknown")
            w.writerow({k: out.get(k, "") for k in csv_fields})

    # HTML output (no OCID/compartment)
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    generate_html(html_name, amd_rows, intel_rows, generated_at)

    print("🎉 KING KAI finished.")
    print(f"🗒️  CSV saved:  {csv_name}")
    print(f"🖼️  HTML saved: {html_name}")


if __name__ == "__main__":
    main()
