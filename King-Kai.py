#!/usr/bin/env python3
"""
👑 KING KAI — Shapes Upgrade Report (timestamped outputs, AMD/Intel/ARM split, +Creator + Pricing/Deltas)

Run:
  python3 king-kai.py

At execution time, KING KAI lists the tenancy's subscribed regions and asks whether to scan:
  1) one selected region
  2) multiple selected regions
  3) all subscribed regions

Outputs (auto-named, UTC timestamp):
  - king-kaiYYYYMMDD-HHMMSS.html
  - king-kaiYYYYMMDD-HHMMSS.csv

Updates in this version:
  1) ARM Ampere support: A1.Flex flagged as legacy (Critical risk).
     Upgrade targets: A2.Flex (with cost delta) and A3.Flex (availability check only).
     Separate ARM table in HTML/CSV with its own availability + delta columns.
  2) Creator HTML cleanup: strips email domain (e.g. mike.smith@company.com → mike.smith in HTML, full in CSV)
  3) Multi-region selection: lists subscribed OCI regions, prompts the user to select one, multiple, or all regions,
     and creates region-specific Compute/Limits clients for each selected region.
  4) Region column: populated from the actual selected OCI region being scanned.

Important:
  - "Creator" is best-effort from tags (createdBy/creator/owner...). If missing, shows "Unknown".
  - Costs are baseline monthly USD estimates based on your Jan-2026 OCI Calculator table.
  - Availability checks are best-effort: shape catalog + quota signals (when accessible).
"""

import oci
import argparse
import csv
import re
import sys
import html as htmlmod
from datetime import datetime, timezone
from pathlib import Path
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
    # ARM Ampere A1
    "VM.Standard.A1.Flex",
}

# Upgrade targets required
E5_TARGET   = "VM.Standard.E5.Flex"
E6_TARGET   = "VM.Standard.E6.Flex"
STD3_TARGET = "VM.Standard3.Flex"
OPT3_TARGET = "VM.Optimized3.Flex"
A2_TARGET   = "VM.Standard.A2.Flex"   # ARM upgrade target (with cost delta)
A3_TARGET   = "VM.Standard.A3.Flex"   # ARM availability-only check column

RISK_ORDER = {"Critical": 0, "High": 1, "Medium": 2}
AVAIL_AVAILABLE = "available"
AVAIL_UNAVAILABLE = "unavailable"
AVAIL_UNKNOWN = "unknown"
TERMINATED_STATES = {"TERMINATED", "TERMINATING"}


# ------------------------------------------------------------
#  Pricing baseline (Monthly USD) — from your OCI Calculator table
# ------------------------------------------------------------
FLEX_BASE_OCPU = 1.0
FLEX_BASE_MEM_GB = 8.0

AMD_FLEX_PRICING = {
    "E2":  {"base": 27.0,  "extra_ocpu": 14.5,  "extra_gb": 1.0},
    "E3":  {"base": 27.0,  "extra_ocpu": 18.0,  "extra_gb": 1.0},
    "E4":  {"base": 27.0,  "extra_ocpu": 20.0,  "extra_gb": 1.0},
    "E5":  {"base": 34.0,  "extra_ocpu": 22.5,  "extra_gb": 1.5},
    "E6":  {"base": 34.0,  "extra_ocpu": 22.5,  "extra_gb": 1.5},
}

INTEL_FLEX_PRICING = {
    "STD3": {"base": 39.0, "extra_ocpu": 30.0, "extra_gb": 1.1},
    "OPT3": {"base": 49.0, "extra_ocpu": 40.0, "extra_gb": 1.1},
}

# ARM Ampere pricing (A1 old / A2 new)
ARM_FLEX_PRICING = {
    "A1": {"base": 18.0, "extra_ocpu": 6.0,  "extra_gb": 0.9},   # A1.Flex approximate baseline
    "A2": {"base": 22.0, "extra_ocpu": 8.0,  "extra_gb": 1.0},   # A2.Flex approximate baseline
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
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


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


def availability_to_csv(status: Optional[str]) -> str:
    if status == AVAIL_AVAILABLE:
        return "Y"
    if status == AVAIL_UNAVAILABLE:
        return "N"
    return "Unknown"


def delta_class(value: Optional[float]) -> str:
    if value is None:
        return "delta-unknown"
    return "delta-pos" if value >= 0 else "delta-neg"


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

    # Root / tenancy name
    try:
        tenancy = identity_client.get_tenancy(tenancy_id).data
        comp_name_by_id[tenancy_id] = getattr(tenancy, "name", "tenancy-root")
    except Exception:
        comp_name_by_id[tenancy_id] = "tenancy-root"

    comp_ids.append(tenancy_id)
    return comp_ids, comp_name_by_id


def config_for_region(config: Dict[str, Any], region_name: str) -> Dict[str, Any]:
    """Return a copy of the OCI config pinned to a specific region."""
    region_config = dict(config)
    region_config["region"] = region_name
    return region_config


def list_subscribed_regions(identity_client, tenancy_id: str, config_region: str) -> List[Dict[str, Any]]:
    """
    Return subscribed OCI regions for the tenancy.
    Uses Identity list_region_subscriptions; falls back to the config region if the call is unavailable.
    """
    regions: List[Dict[str, Any]] = []

    try:
        try:
            response = oci.pagination.list_call_get_all_results(
                identity_client.list_region_subscriptions,
                tenancy_id=tenancy_id,
            )
            data = response.data
        except TypeError:
            # Older OCI SDK signatures may not accept tenancy_id as a keyword.
            response = identity_client.list_region_subscriptions(tenancy_id)
            data = response.data

        for item in data:
            region_name = getattr(item, "region_name", None)
            if not region_name:
                continue
            regions.append({
                "name": str(region_name),
                "status": str(getattr(item, "status", "") or ""),
                "is_home_region": bool(getattr(item, "is_home_region", False)),
            })
    except Exception as e:
        print(f"⚠️ Could not list subscribed regions via Identity API: {e}")

    # Ensure the configured region is always selectable as a safe fallback.
    existing = {r["name"] for r in regions}
    if config_region and config_region not in existing and config_region != "unknown":
        regions.append({"name": config_region, "status": "CONFIG", "is_home_region": False})

    # Deduplicate while preserving useful metadata.
    deduped: Dict[str, Dict[str, Any]] = {}
    for r in regions:
        deduped[r["name"]] = r

    # Keep a stable, readable order: home region first, then alphabetical.
    return sorted(deduped.values(), key=lambda r: (not r.get("is_home_region", False), r.get("name", "")))


def _parse_region_numbers(raw: str, max_index: int) -> List[int]:
    """Parse comma-separated region numbers like '2,3,6' into zero-based indexes."""
    selected: List[int] = []
    seen: Set[int] = set()

    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"'{token}' is not a number")
        idx = int(token) - 1
        if idx < 0 or idx >= max_index:
            raise ValueError(f"Region number {token} is outside the available range 1-{max_index}")
        if idx not in seen:
            selected.append(idx)
            seen.add(idx)

    if not selected:
        raise ValueError("No region numbers were selected")
    return selected


def prompt_region_selection(subscribed_regions: List[Dict[str, Any]]) -> List[str]:
    """Interactive region-selection flow for Cloud Shell / terminal execution."""
    if not subscribed_regions:
        print("❌ No subscribed regions were found and no config-region fallback is available. Exiting.")
        sys.exit(1)

    print("🌍 Subscribed OCI regions detected:")
    for idx, region in enumerate(subscribed_regions, start=1):
        badges: List[str] = []
        if region.get("is_home_region"):
            badges.append("home")
        if region.get("status"):
            badges.append(str(region.get("status")))
        suffix = f" ({', '.join(badges)})" if badges else ""
        print(f"  {idx:>2}) {region['name']}{suffix}")

    print()
    print("Select scan scope:")
    print("  1) Scan one region")
    print("  2) Scan multiple regions, for example: 2,3,6")
    print("  3) Scan all subscribed regions")

    selected_regions: List[str] = []
    while True:
        mode = input("Enter option 1, 2, or 3: ").strip()

        if mode == "1":
            raw = input("Enter the region number to scan: ").strip()
            try:
                indexes = _parse_region_numbers(raw, len(subscribed_regions))
                if len(indexes) != 1:
                    print("❌ Option 1 accepts exactly one region number.")
                    continue
                selected_regions = [subscribed_regions[indexes[0]]["name"]]
                break
            except ValueError as e:
                print(f"❌ {e}")

        elif mode == "2":
            raw = input("Enter region numbers to scan, separated by commas: ").strip()
            try:
                indexes = _parse_region_numbers(raw, len(subscribed_regions))
                selected_regions = [subscribed_regions[i]["name"] for i in indexes]
                break
            except ValueError as e:
                print(f"❌ {e}")

        elif mode == "3":
            selected_regions = [r["name"] for r in subscribed_regions]
            break

        else:
            print("❌ Invalid option. Please enter 1, 2, or 3.")

    print()
    print(f"Selected region(s): {', '.join(selected_regions)}")
    confirm = input("Proceed with scan? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Scan cancelled by user.")
        sys.exit(0)

    return selected_regions


def resolve_regions_from_cli(args, subscribed_regions: List[Dict[str, Any]]) -> Optional[List[str]]:
    """Optional non-interactive mode for automation/CI usage."""
    subscribed_names = [r["name"] for r in subscribed_regions]
    subscribed_set = set(subscribed_names)

    if getattr(args, "all_regions", False):
        if not subscribed_names:
            print("❌ --all-regions was requested, but no subscribed regions were found. Exiting.")
            sys.exit(1)
        if not getattr(args, "yes", False):
            print(f"Selected region(s): {', '.join(subscribed_names)}")
            confirm = input("Proceed with scan? [y/N]: ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("Scan cancelled by user.")
                sys.exit(0)
        return subscribed_names

    raw_regions = getattr(args, "regions", None)
    if not raw_regions:
        return None

    requested = [r.strip() for r in raw_regions.split(",") if r.strip()]
    if not requested:
        print("❌ --regions was provided but no region names were parsed. Exiting.")
        sys.exit(1)

    unknown = [r for r in requested if r not in subscribed_set]
    if unknown:
        print(f"❌ Region(s) not found in tenancy subscriptions: {', '.join(unknown)}")
        print(f"Available region(s): {', '.join(subscribed_names)}")
        sys.exit(1)

    if not getattr(args, "yes", False):
        print(f"Selected region(s): {', '.join(requested)}")
        confirm = input("Proceed with scan? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Scan cancelled by user.")
            sys.exit(0)

    return requested



# Mapping from OCI region slug keywords → friendly display name
_REGION_FRIENDLY: List[Tuple[str, str]] = [
    # Europe
    ("frankfurt",    "Frankfurt"),
    ("amsterdam",    "Amsterdam"),
    ("stockholm",    "Stockholm"),
    ("london",       "London"),
    ("cardiff",      "Cardiff"),
    ("paris",        "Paris"),
    ("marseille",    "Marseille"),
    ("madrid",       "Madrid"),
    ("milan",        "Milan"),
    ("zurich",       "Zurich"),
    ("warsaw",       "Warsaw"),
    # Americas
    ("ashburn",      "Ashburn"),
    ("phoenix",      "Phoenix"),
    ("chicago",      "Chicago"),
    ("sanjose",      "San Jose"),
    ("montreal",     "Montreal"),
    ("toronto",      "Toronto"),
    ("santiago",     "Santiago"),
    ("saopaulo",     "Sao Paulo"),
    ("vinhedo",      "Vinhedo"),
    ("bogota",       "Bogota"),
    ("queretaro",    "Queretaro"),
    ("newport",      "Newport"),
    # Asia Pacific
    ("tokyo",        "Tokyo"),
    ("osaka",        "Osaka"),
    ("seoul",        "Seoul"),
    ("chuncheon",    "Chuncheon"),
    ("sydney",       "Sydney"),
    ("melbourne",    "Melbourne"),
    ("singapore",    "Singapore"),
    ("mumbai",       "Mumbai"),
    ("hyderabad",    "Hyderabad"),
    ("dubai",        "Dubai"),
    ("abudhabi",     "Abu Dhabi"),
    ("jerusalem",    "Jerusalem"),
    ("jeddah",       "Jeddah"),
    # Gov / other
    ("langley",      "Langley"),
    ("luke",         "Luke"),
    ("cheyenne",     "Cheyenne"),
    ("manassas",     "Manassas"),
]


def region_from_ad(ad: Optional[str], config_region: str) -> str:
    """
    Derive a friendly region display name from an availability_domain string.
    AD format examples:
      "eu-frankfurt-1:AD-1"          → "Frankfurt"
      "eu-frankfurt-1-ad-1"          → "Frankfurt"   (your observed format)
      "ABC:EU-FRANKFURT-1-AD-1"      → "Frankfurt"
    Falls back to config_region slug if no friendly name matches.
    """
    if not ad:
        slug = config_region
    else:
        # Normalise: lowercase, replace colons with dashes, collapse whitespace
        normalised = ad.lower().replace(":", "-").replace(" ", "")
        # Strip trailing AD suffix like "-ad-1" or "-ad1"
        slug = re.sub(r"-ad-?\d+$", "", normalised).strip("-")

    slug_lower = slug.lower().replace("-", "").replace("_", "")

    for keyword, friendly in _REGION_FRIENDLY:
        if keyword.lower().replace("-", "") in slug_lower:
            return friendly

    # No match: return the cleaned slug as-is (better than "unknown")
    return slug if slug else config_region


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
#  Creator extraction / formatting
# ------------------------------------------------------------
def get_creator_from_tags(freeform: Optional[Dict[str, Any]], defined: Optional[Dict[str, Any]]) -> str:
    """Best-effort "Creator" from tags."""
    candidates = {
        "creator", "createdby", "created_by", "created-by", "createdbyemail",
        "owner", "created", "created_by_email", "createdbyemailaddress"
    }

    ff = freeform or {}
    df = defined or {}

    def iter_defined():
        for ns, d in df.items():
            if isinstance(d, dict):
                for k, v in d.items():
                    yield k, v

    for k, v in ff.items():
        if str(k).strip().lower() in candidates and v:
            return str(v)

    for k, v in iter_defined():
        if str(k).strip().lower() in candidates and v:
            return str(v)

    return "Unknown"


def creator_for_html(creator: str) -> str:
    """
    Rules:
      - Remove "default/" and domain-ish path for human users (keep only final username part)
      - Strip email domain: mike.smith@company.com → mike.smith
      - Keep full string for OCIDs or principals/services/processes
    """
    if not creator or creator == "Unknown":
        return "Unknown"

    c = creator.strip()

    # Keep full if OCID
    if c.lower().startswith("ocid1."):
        return c

    # Keep full if looks like a principal/service/process
    low = c.lower()
    if any(tok in low for tok in ["instanceprincipal", "resourceprincipal", "principal",
                                   "serviceaccount", "automation", "pipeline", "process"]):
        return c

    # --- FEATURE 2: Strip email domain ---
    # If it's an email address (contains @), keep only the local part (before @)
    if "@" in c:
        c = c.split("@")[0]

    # Normalize separators to "/" and remove surrounding spaces
    norm = re.sub(r"\s+", "", c)
    norm = norm.replace("\\", "/").replace("|", "/").replace(":", "/")

    # Strip leading "default/" (case-insensitive) if present
    norm = re.sub(r"^default/", "", norm, flags=re.IGNORECASE)

    # If still has path segments, keep only the LAST segment for human users
    parts = [p for p in norm.split("/") if p]
    if parts:
        return parts[-1]

    # Fallback
    fallback = re.sub(r"^\s*default\s*[/\\:|]\s*", "", c, flags=re.IGNORECASE)
    return fallback.strip() or c


# ------------------------------------------------------------
#  Classification + Risk
# ------------------------------------------------------------
def is_amd_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard\.E2\.\d+$", shape)) or shape in ("VM.Standard.E3.Flex", "VM.Standard.E4.Flex")


def is_intel_old(shape: str) -> bool:
    return bool(re.match(r"^VM\.Standard2\.\d+$", shape))


def is_arm_old(shape: str) -> bool:
    """A1.Flex is the legacy ARM shape."""
    return shape == "VM.Standard.A1.Flex"


def risk_for_shape(shape: str) -> str:
    """
    CRITICAL:
      - VM.Standard.E2.1
      - VM.Standard.A1.Flex  (A1 → upgrade to same family A2: flagged Critical)
    HIGH:
      - AMD VM.Standard.E2.*
      - AMD VM.Standard.E3.Flex
    MEDIUM:
      - everything else in old list (E4.Flex, Intel Standard2, etc.)
      - ARM A1.Flex upgrade path to A2 is Medium (captured separately)
    """
    if shape == "VM.Standard.E2.1":
        return "Critical"
    if shape == "VM.Standard.A1.Flex":
        return "Critical"          # A1 → A2 upgrade risk is Critical
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
    if shape == "VM.Standard.A1.Flex":
        return linear_flex_cost(ARM_FLEX_PRICING["A1"], ocpu, mem_gb)
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
    if target_shape == A2_TARGET:
        return linear_flex_cost(ARM_FLEX_PRICING["A2"], ocpu, mem_gb)
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
    elif target_shape == A2_TARGET:
        prefixes = ["standard-a2"]
    elif target_shape == A3_TARGET:
        prefixes = ["standard-a3"]
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


def list_shapes_in_ad(compute_client, tenancy_id: str, ad: str) -> Optional[Set[str]]:
    try:
        shapes = oci.pagination.list_call_get_all_results(
            compute_client.list_shapes,
            compartment_id=tenancy_id,
            availability_domain=ad
        ).data
        return {getattr(s, "shape", "") for s in shapes if getattr(s, "shape", "")}
    except Exception:
        return None


def evaluate_upgrade_option(
    target_shape: str,
    instance_ad: Optional[str],
    instance_compartment_id: str,
    required_ocpu: Optional[float],
    required_mem_gb: Optional[float],
    shapes_cache_by_ad: Dict[str, Optional[Set[str]]],
    target_to_limits: Dict[str, List[str]],
    limits_client,
    service_name: str,
    ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]],
) -> str:
    if not instance_ad:
        return AVAIL_UNKNOWN

    ad_shapes = shapes_cache_by_ad.get(instance_ad)
    if ad_shapes is None:
        return AVAIL_UNKNOWN
    if target_shape not in ad_shapes:
        return AVAIL_UNAVAILABLE

    limit_names = target_to_limits.get(target_shape, [])
    if not limit_names:
        return AVAIL_UNKNOWN

    saw_unknown = False
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
        if not ra:
            saw_unknown = True
            continue

        eff = ra.get("effective_quota_value", None)
        avail = ra.get("available", None)
        if eff == 0 or avail == 0:
            return AVAIL_UNAVAILABLE

        required = None
        ln_lower = ln.lower()
        if "core" in ln_lower:
            required = required_ocpu
        elif "memory" in ln_lower:
            required = required_mem_gb

        if required is not None and avail is not None:
            try:
                if float(avail) < float(required):
                    return AVAIL_UNAVAILABLE
            except (TypeError, ValueError):
                saw_unknown = True
        elif required is not None:
            saw_unknown = True

    return AVAIL_UNKNOWN if saw_unknown else AVAIL_AVAILABLE


# ------------------------------------------------------------
#  Scan
# ------------------------------------------------------------
def scan_compartment_shapes_only(
    comp_id: str,
    compute_client,
    active_region: str,
    out_rows: List[Dict[str, Any]],
    include_terminated: bool = False,
) -> int:
    """Returns how many legacy-shape instances matched in this compartment for the active region."""
    before = len(out_rows)

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

        if not include_terminated and str(lifecycle or "").upper() in TERMINATED_STATES:
            continue

        ocpus, mem_gb = infer_ocpu_mem(shape, shape_config)
        cur_cost = current_monthly_cost(shape, ocpus, mem_gb)
        creator  = get_creator_from_tags(ff_tags, df_tags)

        # Region is the actual OCI region being scanned with the region-specific Compute client.
        instance_region = active_region

        # Determine category
        if is_amd_old(shape):
            category = "AMD"
        elif is_intel_old(shape):
            category = "Intel"
        elif is_arm_old(shape):
            category = "ARM"
        else:
            category = "Other"

        out_rows.append({
            "name": name,
            "shape": shape,
            "availability_domain": ad,
            "compartment_id": comp_id,
            "lifecycle_state": lifecycle,
            "ocid": ocid,
            "risk": risk_for_shape(shape),
            "category": category,
            "ocpus": ocpus,
            "mem_gb": mem_gb,
            "current_cost": cur_cost,
            "creator": creator,
            "region": instance_region,   # per-instance region derived from AD
        })

    return len(out_rows) - before


# ------------------------------------------------------------
#  HTML rendering — KAMI dark-theme style
# ------------------------------------------------------------

RISK_BADGE = {
    "Critical": "<span class='risk-badge risk-crit'>Critical</span>",
    "High":     "<span class='risk-badge risk-high'>High</span>",
    "Medium":   "<span class='risk-badge risk-med'>Medium</span>",
}

AVAIL_ICON = {
    AVAIL_AVAILABLE: "<span class='avail-ok' title='Available'>&check;</span>",
    AVAIL_UNAVAILABLE: "<span class='avail-no' title='Unavailable'>&times;</span>",
    AVAIL_UNKNOWN: "<span class='avail-unknown' title='Unknown'>?</span>",
}


def risk_badge(risk: str) -> str:
    return RISK_BADGE.get(risk, f"<span class='risk-badge risk-med'>{esc(risk)}</span>")


def avail_cell(status: Optional[str]) -> str:
    return AVAIL_ICON.get(status or AVAIL_UNKNOWN, AVAIL_ICON[AVAIL_UNKNOWN])


def _html_table(title: str, accent_var: str, rows: List[Dict[str, Any]],
                headers: List[str], row_fn) -> str:
    count = len(rows)
    rows_html = "\n".join(row_fn(r) for r in rows)
    thead = "".join(f"<th>{h}</th>" for h in headers)
    return f"""
<div class="cat-section">
  <div class="cat-header">
    <div class="cat-title">
      <span class="cat-dot" style="background:var({accent_var});box-shadow:0 0 8px var({accent_var})"></span>
      <h2>{esc(title)}</h2>
    </div>
    <span class="cat-badge">{count} instance{'s' if count != 1 else ''}</span>
  </div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr>{thead}</tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""


def html_table_amd(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "Risk", "oCPU", "Mem GB", "Shape", "Instance Name",
        "Creator", "Lifecycle", "Region", "Cost/mo",
        f"E5 avail", f"E6 avail", "E5/E6 Δ/mo"
    ]

    def row_fn(r):
        risk = r.get("risk", "Medium")
        e5 = avail_cell(r.get("e5_status"))
        e6 = avail_cell(r.get("e6_status"))
        delta = fmt_delta(r.get("e56_delta"))
        delta_cls = delta_class(r.get("e56_delta"))
        search_val = " ".join(filter(None, [
            r.get("shape", ""), r.get("name", ""),
            creator_for_html(r.get("creator", "")), r.get("region", "")
        ])).lower()
        return (
            f"<tr class='row-{risk.lower()}' data-search='{esc(search_val)}'>"
            f"<td>{risk_badge(risk)}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('ocpus')))}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('mem_gb')))}</td>"
            f"<td class='mono shape-cell-amd'>{esc(r['shape'])}</td>"
            f"<td class='name-cell'>{esc(r['name'])}</td>"
            f"<td class='creator-cell'>{esc(creator_for_html(r.get('creator','Unknown')))}</td>"
            f"<td><span class='lc-badge'>{esc(r.get('lifecycle_state',''))}</span></td>"
            f"<td class='mono region-cell'>{esc(r.get('region',''))}</td>"
            f"<td class='num cost-cell'>{esc(fmt_money(r.get('current_cost')))}</td>"
            f"<td class='avail-cell'>{e5}</td>"
            f"<td class='avail-cell'>{e6}</td>"
            f"<td class='num {delta_cls}'>{esc(delta)}</td>"
            "</tr>"
        )

    return _html_table("AMD Old Instances", "--amd-accent", rows, headers, row_fn)


def html_table_intel(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "Risk", "oCPU", "Mem GB", "Shape", "Instance Name",
        "Creator", "Lifecycle", "Region", "Cost/mo",
        "STD3 avail", "OPT3 avail", "Best Δ/mo"
    ]

    def row_fn(r):
        risk = r.get("risk", "Medium")
        s3 = avail_cell(r.get("std3_status"))
        o3 = avail_cell(r.get("opt3_status"))
        delta = fmt_delta(r.get("best_intel_delta"))
        delta_cls = delta_class(r.get("best_intel_delta"))
        search_val = " ".join(filter(None, [
            r.get("shape", ""), r.get("name", ""),
            creator_for_html(r.get("creator", "")), r.get("region", "")
        ])).lower()
        return (
            f"<tr class='row-{risk.lower()}' data-search='{esc(search_val)}'>"
            f"<td>{risk_badge(risk)}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('ocpus')))}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('mem_gb')))}</td>"
            f"<td class='mono shape-cell'>{esc(r['shape'])}</td>"
            f"<td class='name-cell'>{esc(r['name'])}</td>"
            f"<td class='creator-cell'>{esc(creator_for_html(r.get('creator','Unknown')))}</td>"
            f"<td><span class='lc-badge'>{esc(r.get('lifecycle_state',''))}</span></td>"
            f"<td class='mono region-cell'>{esc(r.get('region',''))}</td>"
            f"<td class='num cost-cell'>{esc(fmt_money(r.get('current_cost')))}</td>"
            f"<td class='avail-cell'>{s3}</td>"
            f"<td class='avail-cell'>{o3}</td>"
            f"<td class='num {delta_cls}'>{esc(delta)}</td>"
            "</tr>"
        )

    return _html_table("Intel Old Instances", "--intel-accent", rows, headers, row_fn)


def html_table_arm(rows: List[Dict[str, Any]]) -> str:
    """ARM Ampere A1 instances — upgrade availability to A2 and A3."""
    headers = [
        "Risk", "oCPU", "Mem GB", "Shape", "Instance Name",
        "Creator", "Lifecycle", "Region", "Cost/mo",
        "A2 avail", "A3 avail", "A2 Δ/mo"
    ]

    def row_fn(r):
        risk = r.get("risk", "Medium")
        a2 = avail_cell(r.get("a2_status"))
        a3 = avail_cell(r.get("a3_status"))
        delta = fmt_delta(r.get("a2_delta"))
        delta_cls = delta_class(r.get("a2_delta"))
        search_val = " ".join(filter(None, [
            r.get("shape", ""), r.get("name", ""),
            creator_for_html(r.get("creator", "")), r.get("region", "")
        ])).lower()
        return (
            f"<tr class='row-{risk.lower()}' data-search='{esc(search_val)}'>"
            f"<td>{risk_badge(risk)}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('ocpus')))}</td>"
            f"<td class='num'>{esc(fmt_num(r.get('mem_gb')))}</td>"
            f"<td class='mono shape-cell'>{esc(r['shape'])}</td>"
            f"<td class='name-cell'>{esc(r['name'])}</td>"
            f"<td class='creator-cell'>{esc(creator_for_html(r.get('creator','Unknown')))}</td>"
            f"<td><span class='lc-badge'>{esc(r.get('lifecycle_state',''))}</span></td>"
            f"<td class='mono region-cell'>{esc(r.get('region',''))}</td>"
            f"<td class='num cost-cell'>{esc(fmt_money(r.get('current_cost')))}</td>"
            f"<td class='avail-cell'>{a2}</td>"
            f"<td class='avail-cell'>{a3}</td>"
            f"<td class='num {delta_cls}'>{esc(delta)}</td>"
            "</tr>"
        )

    return _html_table("ARM Ampere Old Instances", "--arm-accent", rows, headers, row_fn)


def sort_rows_for_html(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Critical first, then High, then Medium. Within each risk: shape desc, then name."""
    rows2 = sorted(rows, key=lambda x: x.get("name", ""))
    rows2 = sorted(rows2, key=lambda x: x.get("shape", ""), reverse=True)
    rows2 = sorted(rows2, key=lambda x: RISK_ORDER.get(x.get("risk", "Medium"), 9))
    return rows2


# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="👑 KING KAI — Shapes Upgrade Report")
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name from ~/.oci/config (default: DEFAULT)")
    parser.add_argument("--output-dir", default=".", help="Directory to write reports (default: current directory)")
    region_group = parser.add_mutually_exclusive_group()
    region_group.add_argument(
        "--regions",
        default=None,
        help="Optional non-interactive mode: comma-separated OCI region names, e.g. eu-frankfurt-1,il-jerusalem-1",
    )
    region_group.add_argument(
        "--all-regions",
        action="store_true",
        help="Optional non-interactive mode: scan all subscribed regions",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation when using --regions or --all-regions",
    )
    parser.add_argument(
        "--include-terminated",
        action="store_true",
        help="Include TERMINATED and TERMINATING instances in the report",
    )
    args = parser.parse_args()

    try:
        config = oci.config.from_file(profile_name=args.profile)
    except Exception as e:
        print(f"❌ Failed to load OCI config for profile '{args.profile}': {e}")
        sys.exit(1)

    tenancy_id = config.get("tenancy")
    if not tenancy_id:
        print("❌ Couldn't find 'tenancy' in OCI config. Exiting.")
        sys.exit(1)

    config_region = config.get("region", "unknown")
    base_identity_client = oci.identity.IdentityClient(config)

    # Resolve the executing user from Cloud Shell environment variables
    import os
    executing_user = "Unknown"
    # OCI_CS_USER_OCID is always set in Cloud Shell — try to resolve the name via API
    cs_user_ocid = os.environ.get("OCI_CS_USER_OCID")
    if cs_user_ocid:
        try:
            u = base_identity_client.get_user(cs_user_ocid).data
            executing_user = (
                getattr(u, "name", None)
                or getattr(u, "email", None)
                or cs_user_ocid
            )
        except Exception:
            executing_user = cs_user_ocid  # show raw OCID if API fails
    # Fallback: extract username from HOME path (e.g. /home/omri_moas → omri_moas)
    if executing_user == "Unknown":
        home = os.environ.get("HOME", "")
        if home:
            executing_user = home.rstrip("/").split("/")[-1] or "Unknown"

    stamp    = now_stamp_utc()
    base_name = f"king-kai{stamp}"
    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{base_name}.csv"
    html_path = out_dir / f"{base_name}.html"

    subscribed_regions = list_subscribed_regions(base_identity_client, tenancy_id, config_region)
    selected_regions = resolve_regions_from_cli(args, subscribed_regions)
    if selected_regions is None:
        selected_regions = prompt_region_selection(subscribed_regions)

    compartments, comp_name_by_id = collect_all_compartments(base_identity_client, tenancy_id)

    print(f"👑 KING KAI scanning tenancy: {tenancy_id}")
    print(f"🌍 Config region: {config_region}")
    print(f"🌍 Selected region(s): {', '.join(selected_regions)}")
    print(f"📦 Compartments to scan: {len(compartments)} (including root)")
    print(f"🧠 Legacy shapes tracked: {len(OLD_SHAPES_SET)}")
    print("-" * 70)

    all_rows: List[Dict[str, Any]] = []
    total_compartments = len(compartments)

    for region_idx, region_name in enumerate(selected_regions, start=1):
        print()
        print(f"🌍 [{region_idx}/{len(selected_regions)}] Scanning region: {region_name}")
        print("-" * 70)

        region_config = config_for_region(config, region_name)
        region_identity_client = oci.identity.IdentityClient(region_config)
        region_compute_client  = oci.core.ComputeClient(region_config)
        region_limits_client   = oci.limits.LimitsClient(region_config)
        availability_domains   = list_availability_domains(region_identity_client, tenancy_id)

        region_rows_before = len(all_rows)

        for idx, comp_id in enumerate(compartments, start=1):
            cname = comp_name_by_id.get(comp_id, comp_id)
            print(f"[{idx:>3}/{total_compartments}] Scanning compartment: {cname} ({comp_id}) ... ", end="", flush=True)
            found = 0
            try:
                found = scan_compartment_shapes_only(
                    comp_id,
                    region_compute_client,
                    region_name,
                    all_rows,
                    include_terminated=args.include_terminated,
                )
                print(f"done (found {found})", flush=True)
            except oci.exceptions.ServiceError as e:
                print(f"skipped (ServiceError: {e.status})", flush=True)
            except Exception as e:
                print(f"skipped (Error: {e})", flush=True)

        region_rows = [r for r in all_rows[region_rows_before:] if r.get("region") == region_name]
        print(f"🔎 Region scan complete: {region_name} | old instances found: {len(region_rows)}")

        if not region_rows:
            continue

        print(f"🧪 Checking upgrade-shape availability in {region_name} ...")

        # Build region-specific caches for upgrade availability checks.
        ads_to_check: Set[str] = set([r["availability_domain"] for r in region_rows if r.get("availability_domain")]) or set(availability_domains)
        shapes_cache_by_ad: Dict[str, Optional[Set[str]]] = {}
        for ad in sorted([a for a in ads_to_check if a]):
            shapes_cache_by_ad[ad] = list_shapes_in_ad(region_compute_client, tenancy_id, ad)

        service_name    = discover_compute_service_name(region_limits_client, tenancy_id)
        all_limit_names = list_limit_names(region_limits_client, tenancy_id, service_name)

        targets = [E5_TARGET, E6_TARGET, STD3_TARGET, OPT3_TARGET, A2_TARGET, A3_TARGET]
        target_to_limits: Dict[str, List[str]] = {t: find_limit_names_for_target(all_limit_names, t) for t in targets}
        ra_cache: Dict[Tuple[str, str, Optional[str]], Optional[Dict[str, Any]]] = {}

        # Compute availability + deltas per instance, using only this region's clients/caches.
        for r in region_rows:
            ad       = r.get("availability_domain")
            comp_id  = r.get("compartment_id", "")
            ocpu     = r.get("ocpus")
            mem_gb   = r.get("mem_gb")
            cur_cost = r.get("current_cost")

            if r.get("category") == "AMD":
                r["e5_status"] = evaluate_upgrade_option(
                    E5_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                r["e6_status"] = evaluate_upgrade_option(
                    E6_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                e5_cost       = target_monthly_cost(E5_TARGET, ocpu, mem_gb)
                r["e56_delta"] = round(e5_cost - cur_cost, 2) if (e5_cost is not None and cur_cost is not None) else None

            elif r.get("category") == "Intel":
                r["std3_status"] = evaluate_upgrade_option(
                    STD3_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                r["opt3_status"] = evaluate_upgrade_option(
                    OPT3_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                std3_cost  = target_monthly_cost(STD3_TARGET, ocpu, mem_gb)
                opt3_cost  = target_monthly_cost(OPT3_TARGET, ocpu, mem_gb)
                std3_delta = round(std3_cost - cur_cost, 2) if (std3_cost is not None and cur_cost is not None) else None
                opt3_delta = round(opt3_cost - cur_cost, 2) if (opt3_cost is not None and cur_cost is not None) else None
                candidates = [d for d in [std3_delta, opt3_delta] if d is not None]
                r["best_intel_delta"] = min(candidates) if candidates else None

            elif r.get("category") == "ARM":
                r["a2_status"] = evaluate_upgrade_option(
                    A2_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                r["a3_status"] = evaluate_upgrade_option(
                    A3_TARGET, ad, comp_id, ocpu, mem_gb, shapes_cache_by_ad,
                    target_to_limits, region_limits_client, service_name, ra_cache
                )
                a2_cost       = target_monthly_cost(A2_TARGET, ocpu, mem_gb)
                r["a2_delta"] = round(a2_cost - cur_cost, 2) if (a2_cost is not None and cur_cost is not None) else None

    print("-" * 70)
    print(f"✅ Scan complete. Total old instances found: {len(all_rows)}")
    print()

    # Executive counts
    amd_counts   = {"E2": 0, "E3": 0, "E4": 0}
    intel_counts = {"Standard2": 0}
    arm_counts   = {"A1": 0}

    for r in all_rows:
        cat = r.get("category", "")
        sh  = r.get("shape", "")
        if cat == "AMD":
            if sh.startswith("VM.Standard.E2."):
                amd_counts["E2"] += 1
            elif sh == "VM.Standard.E3.Flex":
                amd_counts["E3"] += 1
            elif sh == "VM.Standard.E4.Flex":
                amd_counts["E4"] += 1
        elif cat == "Intel":
            intel_counts["Standard2"] += 1
        elif cat == "ARM":
            arm_counts["A1"] += 1

    # HTML ordering
    amd_rows   = sort_rows_for_html([r for r in all_rows if r.get("category") == "AMD"])
    intel_rows = sort_rows_for_html([r for r in all_rows if r.get("category") == "Intel"])
    arm_rows   = sort_rows_for_html([r for r in all_rows if r.get("category") == "ARM"])

    # --------------- Generate CSV (Y/N/Unknown availability, full creator) ----------------
    with open(csv_path, mode="w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "Category", "Risk", "oCPU", "MemoryGB", "Shape", "InstanceName", "Creator", "Lifecycle",
            "Region",
            "CurrentCostMo",
            f"{E5_TARGET}_avail", f"{E6_TARGET}_avail", "E5/E6.Flex monthly add-on",
            f"{STD3_TARGET}_avail", f"{OPT3_TARGET}_avail", "Intel_best_monthly_addon",
            f"{A2_TARGET}_avail", f"{A3_TARGET}_avail", "ARM_A2_monthly_addon",
            "AvailabilityDomain", "CompartmentName", "CompartmentId", "OCID",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for r in all_rows:
            comp_id = r.get("compartment_id", "")
            cat     = r.get("category", "")

            e5_y  = availability_to_csv(r.get("e5_status"))   if cat == "AMD"   else ""
            e6_y  = availability_to_csv(r.get("e6_status"))   if cat == "AMD"   else ""
            s3_y  = availability_to_csv(r.get("std3_status")) if cat == "Intel" else ""
            o3_y  = availability_to_csv(r.get("opt3_status")) if cat == "Intel" else ""
            a2_y  = availability_to_csv(r.get("a2_status"))   if cat == "ARM"   else ""
            a3_y  = availability_to_csv(r.get("a3_status"))   if cat == "ARM"   else ""

            writer.writerow({
                "Category":    cat,
                "Risk":        r.get("risk", ""),
                "oCPU":        fmt_num(r.get("ocpus")),
                "MemoryGB":    fmt_num(r.get("mem_gb")),
                "Shape":       r.get("shape", ""),
                "InstanceName": r.get("name", ""),
                "Creator":     r.get("creator", "Unknown"),   # full value in CSV
                "Lifecycle":   r.get("lifecycle_state", ""),
                "Region":      r.get("region", ""),
                "CurrentCostMo": fmt_money(r.get("current_cost")),
                f"{E5_TARGET}_avail":  e5_y,
                f"{E6_TARGET}_avail":  e6_y,
                "E5/E6.Flex monthly add-on": fmt_delta(r.get("e56_delta"))   if cat == "AMD"   else "",
                f"{STD3_TARGET}_avail": s3_y,
                f"{OPT3_TARGET}_avail": o3_y,
                "Intel_best_monthly_addon":  fmt_delta(r.get("best_intel_delta")) if cat == "Intel" else "",
                f"{A2_TARGET}_avail": a2_y,
                f"{A3_TARGET}_avail": a3_y,
                "ARM_A2_monthly_addon": fmt_delta(r.get("a2_delta"))         if cat == "ARM"   else "",
                "AvailabilityDomain": r.get("availability_domain", ""),
                "CompartmentName": comp_name_by_id.get(comp_id, ""),
                "CompartmentId": comp_id,
                "OCID": r.get("ocid", ""),
            })

    print(f"🗒️ CSV report saved to: {csv_path}")

    # --------------- Generate HTML (KAMI dark-theme) ----------------
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    selected_regions_text = ", ".join(selected_regions)

    amd_section   = html_table_amd(amd_rows)   if amd_rows   else '<div class="empty-note">No AMD old instances found.</div>'
    intel_section = html_table_intel(intel_rows) if intel_rows else '<div class="empty-note">No Intel old instances found.</div>'
    arm_section   = html_table_arm(arm_rows)   if arm_rows   else '<div class="empty-note">No ARM A1 instances found.</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>KING KAI — Shapes Upgrade Advisor</title>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&family=Exo+2:wght@300;400;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:           #070d14;
      --bg2:          #0c1520;
      --bg3:          #111d2a;
      --border:       #1a2e42;
      --border2:      #243d56;
      --accent:       #f0b429;
      --accent2:      #00e5ff;
      --text:         #c8dae8;
      --text-dim:     #4a6a82;
      --text-head:    #e8f4ff;
      --crit:         #ff4d4d;
      --high:         #f0b429;
      --med:          #00c896;
      --ok:           #00c896;
      --no:           #ff4d4d;
      --amd-accent:   #f0b429;
      --intel-accent: #00e5ff;
      --arm-accent:   #a78bfa;
      --font-mono:    'Share Tech Mono', monospace;
      --font-ui:      'Rajdhani', sans-serif;
      --font-body:    'Exo 2', sans-serif;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-body);
      font-size: 13px;
      line-height: 1.5;
      min-height: 100vh;
    }}

    body::before {{
      content: '';
      position: fixed; inset: 0; z-index: 0;
      background-image:
        linear-gradient(rgba(0,229,255,.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,229,255,.03) 1px, transparent 1px);
      background-size: 40px 40px;
      pointer-events: none;
    }}

    .wrap {{ position: relative; z-index: 1; max-width: 1500px; margin: 0 auto; padding: 0 24px 60px; }}

    /* ── Header ── */
    .site-header {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 32px 0 24px;
      border-bottom: 1px solid var(--border2);
      margin-bottom: 32px;
    }}
    .logo-block {{ display: flex; align-items: center; gap: 18px; }}
    .logo-glyph {{
      width: 52px; height: 52px;
      background: linear-gradient(135deg, #001a2e, #003050);
      border: 1px solid var(--accent2);
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-family: var(--font-mono);
      color: var(--accent2);
      font-size: 24px;
      box-shadow: 0 0 20px rgba(0,229,255,.2), inset 0 0 10px rgba(0,229,255,.05);
    }}
    .logo-text h1 {{
      font-family: var(--font-ui);
      font-size: 26px; font-weight: 700; letter-spacing: 6px;
      color: var(--text-head); text-transform: uppercase;
    }}
    .logo-text p {{
      font-family: var(--font-mono);
      font-size: 11px; color: var(--text-dim); letter-spacing: 2px;
      text-transform: uppercase; margin-top: 2px;
    }}
    .header-meta {{
      text-align: right;
      font-family: var(--font-mono);
      font-size: 11px; color: var(--text-dim); line-height: 1.8;
    }}
    .header-meta .ts {{ color: var(--accent2); }}
    .header-meta .user {{ color: var(--text); }}

    /* ── Search bar ── */
    .search-wrap {{
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 0 32px;
    }}
    .search-box {{
      position: relative;
      width: 100%;
      max-width: 520px;
    }}
    .search-box input {{
      width: 100%;
      background: rgba(0,229,255,.04);
      border: 1px solid var(--accent2);
      border-radius: 5px;
      color: var(--text);
      font-family: var(--font-mono);
      font-size: 12px;
      letter-spacing: 1px;
      padding: 8px 36px 8px 14px;
      outline: none;
      transition: box-shadow .2s, background .2s;
    }}
    .search-box input::placeholder {{
      color: var(--text-dim);
      letter-spacing: 1.5px;
    }}
    .search-box input:focus {{
      background: rgba(0,229,255,.07);
      box-shadow: 0 0 12px rgba(0,229,255,.25);
    }}
    .search-icon {{
      position: absolute;
      right: 11px; top: 50%;
      transform: translateY(-50%);
      color: var(--accent2);
      font-size: 13px;
      pointer-events: none;
      opacity: .7;
    }}
    .clear-btn {{
      position: absolute;
      right: 11px; top: 50%;
      transform: translateY(-50%);
      color: var(--text-dim);
      font-size: 16px;
      cursor: pointer;
      display: none;
      background: none;
      border: none;
      line-height: 1;
      padding: 0;
    }}
    .clear-btn:hover {{ color: var(--accent2); }}
    .search-count {{
      font-family: var(--font-mono);
      font-size: 10px;
      color: var(--text-dim);
      text-align: center;
      margin-top: 5px;
      min-height: 14px;
      letter-spacing: 1px;
    }}
    .search-count span {{ color: var(--accent2); }}
    tr.search-hidden {{ display: none; }}

    /* ── Summary cards ── */
    .cards-wrap {{
      display: flex; gap: 14px; flex-wrap: wrap;
      margin-bottom: 36px;
    }}
    .card {{
      background: var(--bg2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 20px;
      min-width: 200px;
      flex: 1;
    }}
    .card-label {{
      font-family: var(--font-mono);
      font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
      color: var(--text-dim); margin-bottom: 6px;
    }}
    .card-value {{
      font-family: var(--font-ui);
      font-size: 22px; font-weight: 700;
      color: var(--text-head);
    }}
    .card-sub {{
      font-family: var(--font-mono);
      font-size: 11px; color: var(--text-dim); margin-top: 4px;
    }}
    .card-sub span {{ color: var(--accent2); }}

    /* ── Category section ── */
    .cat-section {{
      margin-bottom: 40px;
      background: var(--bg2);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
    }}
    .cat-header {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px 24px;
      background: linear-gradient(90deg, #0a1020, #070d14);
      border-bottom: 1px solid var(--border2);
    }}
    .cat-title {{ display: flex; align-items: center; gap: 12px; }}
    .cat-dot {{
      width: 10px; height: 10px; border-radius: 50%;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .4; }} }}
    .cat-icon {{ font-size: 18px; }}
    .cat-title h2 {{
      font-family: var(--font-ui);
      font-size: 17px; font-weight: 600; letter-spacing: 3px;
      color: var(--text-head); text-transform: uppercase;
    }}
    .cat-badge {{
      font-family: var(--font-mono); font-size: 11px;
      color: var(--text-dim); letter-spacing: 1px;
      background: rgba(0,229,255,.06);
      border: 1px solid rgba(0,229,255,.15);
      border-radius: 4px; padding: 3px 10px;
    }}

    .table-wrap {{ overflow-x: auto; }}

    /* ── Data table ── */
    .data-table {{
      width: 100%; border-collapse: collapse;
      font-family: var(--font-mono); font-size: 12px;
    }}
    .data-table thead tr {{ background: rgba(0,229,255,.04); }}
    .data-table th {{
      padding: 9px 12px; text-align: left;
      color: var(--text-dim); font-size: 10px;
      letter-spacing: 1.5px; text-transform: uppercase;
      border-bottom: 1px solid var(--border2);
      font-weight: 400; white-space: nowrap;
    }}
    .data-table td {{
      padding: 8px 12px; vertical-align: middle;
      border-bottom: 1px solid rgba(26,46,66,.6);
    }}
    .data-table tbody tr:hover {{ background: rgba(0,229,255,.04); }}

    /* row risk tints — no background differentiation, badge carries the risk signal */
    .row-critical {{ }}
    .row-high     {{ }}
    .row-medium   {{ }}

    /* ── Risk badge ── */
    .risk-badge {{
      display: inline-block; padding: 2px 9px;
      border-radius: 4px; font-size: 10px; letter-spacing: 1px;
      text-transform: uppercase; font-weight: 600;
    }}
    .risk-crit {{ background: rgba(255,77,77,.18); color: var(--crit); border: 1px solid rgba(255,77,77,.35); }}
    .risk-high {{ background: rgba(240,180,41,.18); color: var(--high); border: 1px solid rgba(240,180,41,.35); }}
    .risk-med  {{ background: rgba(0,200,150,.14); color: var(--med);  border: 1px solid rgba(0,200,150,.3); }}

    /* ── Availability ── */
    .avail-cell {{ text-align: center; }}
    .avail-ok {{ color: var(--ok); font-size: 15px; font-weight: 700; }}
    .avail-no {{ color: var(--no); font-size: 15px; font-weight: 700; }}
    .avail-unknown {{ color: var(--text-dim); font-size: 14px; font-weight: 700; }}

    /* ── Delta ── */
    .delta-pos {{ color: #ff8080; }}
    .delta-neg {{ color: var(--ok); }}
    .delta-unknown {{ color: var(--text-dim); }}

    /* ── Lifecycle badge ── */
    .lc-badge {{
      font-size: 10px; padding: 1px 6px; border-radius: 3px;
      letter-spacing: 1px; text-transform: uppercase;
      background: rgba(0,229,255,.08); color: var(--accent2);
    }}

    /* cell helpers */
    .num {{ text-align: right; }}
    .mono {{ font-family: var(--font-mono); }}
    .shape-cell {{ color: var(--accent2); white-space: nowrap; font-size: 11px; }}
    .shape-cell-amd {{ color: var(--accent2); white-space: nowrap; font-size: 11px; min-width: 160px; }}
    .name-cell  {{ color: var(--text); min-width: 180px; word-break: break-word; }}
    .creator-cell {{ color: var(--text-dim); }}
    .region-cell  {{ color: var(--text-dim); font-size: 11px; }}
    .cost-cell    {{ color: var(--text); font-weight: 600; }}

    /* ── Pricing note ── */
    .pricing-note {{
      margin-bottom: 24px;
      background: rgba(0,229,255,.04);
      border: 1px solid rgba(0,229,255,.18);
      border-radius: 8px;
      padding: 12px 18px;
      font-family: var(--font-mono);
      font-size: 11px; color: var(--text-dim); letter-spacing: .5px;
      line-height: 1.7;
    }}
    .pricing-note strong {{ color: var(--accent2); }}

    .empty-note {{
      padding: 24px;
      font-family: var(--font-mono); font-size: 12px;
      color: var(--text-dim); text-align: center; font-style: italic;
    }}

    /* ── Footer ── */
    .footer {{
      margin-top: 48px; padding-top: 20px;
      border-top: 1px solid var(--border);
      text-align: center;
      font-family: var(--font-mono); font-size: 11px;
      color: var(--text-dim); letter-spacing: 1px;
    }}
    .footer span {{ color: var(--accent); }}
  </style>
</head>
<body>
<div class="wrap">

  <header class="site-header">
    <div class="logo-block">
      <div class="logo-glyph">🜲</div>
      <div class="logo-text">
        <h1>KING KAI</h1>
        <p>OCI Shapes Upgrade Advisor</p>
      </div>
    </div>
    <div class="search-wrap">
      <div class="search-box">
        <input type="text" id="globalSearch" placeholder="FILTER SHAPE, INSTANCE, CREATOR, REGION…" autocomplete="off" spellcheck="false">
        <span class="search-icon" id="searchIcon">⌕</span>
        <button class="clear-btn" id="clearBtn" title="Clear filter">✕</button>
      </div>
      <div class="search-count" id="searchCount"></div>
    </div>
    <div class="header-meta">
      <div class="ts">{esc(generated)}</div>
      <div>Executed by: <span class="user">{esc(creator_for_html(executing_user))}</span></div>
      <div>Regions scanned: <span style="color:var(--accent2)">{esc(selected_regions_text)}</span></div>
      <div>Legacy instances found: <span style="color:var(--accent2)">{len(all_rows)}</span></div>
    </div>
  </header>

  <div class="cards-wrap">
    <div class="card">
      <div class="card-label">AMD Legacy</div>
      <div class="card-value">{amd_counts["E2"] + amd_counts["E3"] + amd_counts["E4"]}</div>
      <div class="card-sub">E2: <span>{amd_counts["E2"]}</span> · E3: <span>{amd_counts["E3"]}</span> · E4: <span>{amd_counts["E4"]}</span></div>
    </div>
    <div class="card">
      <div class="card-label">Intel Legacy</div>
      <div class="card-value">{intel_counts["Standard2"]}</div>
      <div class="card-sub">Standard2: <span>{intel_counts["Standard2"]}</span></div>
    </div>
    <div class="card">
      <div class="card-label">ARM Legacy</div>
      <div class="card-value">{arm_counts["A1"]}</div>
      <div class="card-sub">A1.Flex: <span>{arm_counts["A1"]}</span></div>
    </div>
  </div>

  <div class="pricing-note">
    <strong>ℹ Pricing baseline:</strong> Feb-2026 OCI Calculator monthly USD estimates.
    Actual tenancy billing may differ (discounts, credits, reserved capacity).
    &check; / &times; / ? availability is best-effort (shape catalog + quota signals) and is checked with region-specific Compute/Limits clients.
    Creator column shows username only (email domain stripped); full value preserved in the CSV export.
  </div>

  {amd_section}
  {intel_section}
  {arm_section}

  <footer class="footer">
    Generated by <span>KING KAI</span> · OCI Shapes Upgrade Advisor · {esc(generated)}
  </footer>

</div>

<script>
(function () {{
  var input      = document.getElementById('globalSearch');
  var clearBtn   = document.getElementById('clearBtn');
  var searchIcon = document.getElementById('searchIcon');
  var countEl    = document.getElementById('searchCount');

  function applyFilter() {{
    var term = input.value.trim().toLowerCase();

    if (term) {{
      clearBtn.style.display   = 'block';
      searchIcon.style.display = 'none';
    }} else {{
      clearBtn.style.display   = 'none';
      searchIcon.style.display = 'block';
    }}

    var allRows     = document.querySelectorAll('tr[data-search]');
    var totalRows   = allRows.length;
    var visibleRows = 0;

    allRows.forEach(function (row) {{
      if (!term || row.dataset.search.indexOf(term) !== -1) {{
        row.style.display = '';
        visibleRows++;
      }} else {{
        row.style.display = 'none';
      }}
    }});

    // Show/hide entire cat-section if all its rows are hidden
    document.querySelectorAll('.cat-section').forEach(function (section) {{
      var sectionRows    = section.querySelectorAll('tr[data-search]');
      var sectionVisible = Array.from(sectionRows).some(function (r) {{ return r.style.display !== 'none'; }});
      section.style.display = (!term || sectionVisible) ? '' : 'none';
    }});

    // Update count badge
    if (term) {{
      countEl.innerHTML = '<span>' + visibleRows + '</span> of ' + totalRows + ' instances';
    }} else {{
      countEl.textContent = '';
    }}
  }}

  input.addEventListener('input', applyFilter);

  clearBtn.addEventListener('click', function () {{
    input.value = '';
    applyFilter();
    input.focus();
  }});
}})();
</script>

</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🖼️ HTML report saved to: {html_path}")
    print(f"✅ Done. Output prefix: {base_name}")

if __name__ == "__main__":
    main()
