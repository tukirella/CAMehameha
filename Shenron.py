#!/usr/bin/env python3
"""
Shenron2 - OCI Service Limits Workflow

Purpose:
  - Export current OCI service limits, usage and available capacity to a CSV file.
  - Let the customer review/edit the CSV and enter requested new service limits.
  - Apply explicit increases from an edited CSV or guided terminal entry after a live refresh and preview.

Designed for:
  - OCI Cloud Shell
  - Local machines with OCI CLI configured
  - Different tenants/accounts/team members

CSV workflow:
  1. Run the script and choose "Export current limits to CSV".
  2. Download/open the CSV and fill requested_service_limit for rows to increase.
  3. Set apply to yes for those rows.
  4. Run the script again and choose "Apply limit increases from edited CSV".

Requirements:
  - Python 3
  - OCI CLI configured and authenticated
  - jq is NOT required
  - For creating service limit requests, OCI CLI must support:
      oci limits-increase limits-increase-request create
    If the current Cloud Shell OCI CLI does not support it, apply mode can
    install a newer OCI CLI into ~/oci-cli-latest and continue.
"""

import argparse
import configparser
import csv
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Optional focused OKE shortcut. Normal export mode discovers all OCI limit
# services dynamically from the tenancy/region.
CURATED_LIMITS: Dict[str, List[str]] = {
    "container-engine": [
        "cluster-count",
        "enhanced-cluster-count",
        "node-count",
        "virtual-node-count",
    ],
    "container-registry": [
        "concurrent-pull-count",
        "image-per-repo-count",
        "image-per-tenant-count",
        "repo-count",
        "request-limit-per-second-count",
        "storage-bytes",
    ],
    "load-balancer": [
        "lb-flexible-count",
        "lb-flexible-bandwidth-sum",
    ],
    "compute": [
        "standard-e4-core-count",
        "standard-e4-memory-count",
        "standard-e5-core-count",
        "standard-e5-memory-count",
        "standard-e6-core-count",
        "standard-e6-memory-count",
    ],
}

DEFAULT_SERVICES = list(CURATED_LIMITS.keys())

CSV_COLUMNS = [
    "generated_at_utc",
    "tenancy_id",
    "region",
    "service",
    "service_display_name",
    "limit_name",
    "availability_domain",
    "scope_type",
    "usage",
    "available",
    "current_service_limit",
    "requested_service_limit",
    "requested_increase_delta",
    "apply",
    "notes",
]

REQUIRED_APPLY_COLUMNS = [
    "tenancy_id",
    "region",
    "service",
    "limit_name",
    "availability_domain",
    "current_service_limit",
    "requested_service_limit",
    "apply",
]

APPLY_TRUE_VALUES = {"1", "true", "yes", "y", "apply"}
LATEST_CLI_DIR = "~/oci-cli-latest"
ANSI_BOLD_CYAN = "\033[1;36m"
ANSI_RESET = "\033[0m"


def find_oci_binary(cli_override: Optional[str] = None) -> str:
    """Find an OCI CLI binary, preferring explicit override and local venv."""
    candidates = []

    if cli_override:
        candidates.append(os.path.expanduser(cli_override))

    candidates.append(os.path.expanduser("~/oci-cli-latest/bin/oci"))
    candidates.append(os.path.expanduser("~/oci-cli-latest/Scripts/oci.exe"))
    candidates.append(os.path.expanduser("~/oci-cli-latest/Scripts/oci"))

    found = shutil.which("oci")
    if found:
        candidates.append(found)

    for candidate in candidates:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate

    print("ERROR: OCI CLI was not found.")
    print("Install/configure OCI CLI, or pass --oci-bin /path/to/oci")
    sys.exit(1)


def latest_cli_paths() -> Tuple[str, str]:
    """Return the expected latest-CLI Python and OCI paths for this OS."""
    base_dir = os.path.expanduser(LATEST_CLI_DIR)

    if os.name == "nt":
        return (
            os.path.join(base_dir, "Scripts", "python.exe"),
            os.path.join(base_dir, "Scripts", "oci.exe"),
        )

    return (
        os.path.join(base_dir, "bin", "python"),
        os.path.join(base_dir, "bin", "oci"),
    )


def run_streaming_cmd(cmd: List[str]) -> None:
    """Run a command while streaming output to the terminal."""
    print("\nRunning:")
    print(" ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True)

    if proc.returncode != 0:
        print(f"\nERROR: command failed with exit code {proc.returncode}.")
        sys.exit(proc.returncode)


def resolve_cli_context(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str], str, str]:
    """Resolve CLI options to pass through and config context to read."""
    cli_profile = args.profile or os.environ.get("OCI_CLI_PROFILE")
    cli_config_file = args.config_file or os.environ.get("OCI_CLI_CONFIG_FILE")

    config_profile = cli_profile or "DEFAULT"
    config_file = cli_config_file or "~/.oci/config"

    return cli_profile, cli_config_file, config_profile, config_file


def build_oci_cmd(
    oci_bin: str,
    args: List[str],
    profile: Optional[str] = None,
    config_file: Optional[str] = None,
) -> List[str]:
    """Build an OCI CLI command, preserving the chosen profile/config file."""
    cmd = [oci_bin]

    if profile:
        cmd += ["--profile", profile]

    if config_file:
        cmd += ["--config-file", os.path.expanduser(config_file)]

    return cmd + args


def run_cmd(cmd: List[str], allow_fail: bool = False) -> subprocess.CompletedProcess:
    """Run a command and return CompletedProcess."""
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if proc.returncode != 0 and not allow_fail:
        print("\nERROR running command:")
        print(" ".join(cmd))
        if proc.stderr.strip():
            print(proc.stderr.strip())
        sys.exit(proc.returncode)

    return proc


def run_json(
    oci_bin: str,
    args: List[str],
    profile: Optional[str] = None,
    config_file: Optional[str] = None,
    allow_fail: bool = False,
    warning_context: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Run OCI CLI and parse JSON output."""
    cmd = build_oci_cmd(oci_bin, args + ["--output", "json"], profile, config_file)
    proc = run_cmd(cmd, allow_fail=allow_fail)

    if proc.returncode != 0:
        if warning_context:
            print(f"WARNING: {warning_context}")
            if proc.stderr.strip():
                print(proc.stderr.strip())
        return None

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        if allow_fail:
            return None
        print("ERROR: could not parse OCI CLI JSON output.")
        print(proc.stdout)
        sys.exit(1)


def extract_items(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Handle OCI CLI list responses that may return data as list or data.items."""
    if not payload or "data" not in payload:
        return []

    data = payload["data"]

    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]

    return []


def read_oci_config_value(key: str, profile: str, config_file: str) -> Optional[str]:
    """Read a key from OCI config."""
    config_file = os.path.expanduser(config_file)

    if not os.path.exists(config_file):
        return None

    parser = configparser.ConfigParser()
    parser.read(config_file)

    if profile == parser.default_section or parser.has_section(profile):
        value = parser.get(profile, key, fallback=None)
        if value:
            return value.strip()

    default_value = parser.defaults().get(key)
    if default_value:
        return default_value.strip()

    return None


def resolve_tenancy_and_region(
    args: argparse.Namespace,
    config_profile: str,
    config_file: str,
) -> Tuple[str, str]:
    """Resolve tenancy OCID and region from args, env, or OCI config."""
    tenancy = (
        args.tenancy_id
        or os.environ.get("OCI_CLI_TENANCY")
        or read_oci_config_value("tenancy", config_profile, config_file)
    )

    region = (
        args.region
        or os.environ.get("OCI_CLI_REGION")
        or read_oci_config_value("region", config_profile, config_file)
    )

    if not tenancy:
        print("ERROR: Could not detect tenancy OCID.")
        print("Options:")
        print("  1. Run from OCI Cloud Shell")
        print("  2. Configure OCI CLI")
        print("  3. Pass --tenancy-id ocid1.tenancy...")
        sys.exit(1)

    if not region:
        region = input("Could not detect region. Enter region, for example eu-frankfurt-1: ").strip()

    if not region:
        print("ERROR: region is required.")
        sys.exit(1)

    return tenancy, region


def progress(message: str, enabled: bool = True) -> None:
    """Print progress immediately so Cloud Shell users can see activity."""
    if enabled:
        print(message, flush=True)


def cyan(text: str) -> str:
    """Return cyan terminal text when stdout is interactive."""
    if sys.stdout.isatty():
        return f"{ANSI_BOLD_CYAN}{text}{ANSI_RESET}"

    return text


def parse_services(raw_services: Optional[str]) -> List[str]:
    """Parse a comma-separated service list."""
    if not raw_services:
        return []

    services = [service.strip() for service in raw_services.split(",") if service.strip()]
    if not services:
        print("ERROR: --services was provided but no service names were found.")
        sys.exit(1)

    return services


def service_entries_from_names(services: List[str]) -> List[Dict[str, str]]:
    """Convert service API names to service entry dictionaries."""
    return [{"name": service, "display_name": service} for service in services]


def service_name_from_item(item: Dict[str, Any]) -> Optional[str]:
    """Extract a service API name from OCI CLI output."""
    for key in ("name", "service-name", "serviceName"):
        value = item.get(key)
        if value:
            return str(value)

    return None


def service_display_name_from_item(item: Dict[str, Any], fallback: str) -> str:
    """Extract a human-friendly service name from OCI CLI output."""
    for key in ("display-name", "displayName", "description"):
        value = item.get(key)
        if value:
            return str(value)

    return fallback


def format_service_label(service: Dict[str, str]) -> str:
    """Format a service entry for display."""
    display = service["display_name"]
    name = service["name"]
    return f"{display} ({name})" if display != name else name


def print_service_list(services: List[Dict[str, str]]) -> None:
    """Print discovered services with stable selection numbers."""
    for index, service in enumerate(services, start=1):
        print(f"  {index:3}. {format_service_label(service)}", flush=True)


def discover_limit_services(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    show_progress: bool = True,
    list_services: bool = True,
) -> List[Dict[str, str]]:
    """Discover all OCI services that expose service limits."""
    progress("\nDiscovering OCI services that expose service limits...", show_progress)
    payload = run_json(
        oci_bin,
        [
            "limits", "service", "list",
            "--compartment-id", tenancy_id,
            "--all",
            "--region", region,
        ],
        profile,
        config_file,
        allow_fail=False,
    )

    services: List[Dict[str, str]] = []
    seen = set()

    for item in extract_items(payload):
        service_name = service_name_from_item(item)
        if not service_name or service_name in seen:
            continue

        seen.add(service_name)
        services.append(
            {
                "name": service_name,
                "display_name": service_display_name_from_item(item, service_name),
            }
        )

    services.sort(key=lambda service: (service["display_name"].lower(), service["name"].lower()))

    if not services:
        print("ERROR: OCI returned no limit services for this tenancy/region.")
        print("You can still run with --services service1,service2 if you need to target specific services.")
        sys.exit(1)

    progress(f"Discovered {len(services)} services with limits:", show_progress)
    if show_progress and list_services:
        print_service_list(services)

    return services


def timestamp_for_filename() -> str:
    """Return a compact UTC timestamp for file names."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def default_export_path(region: str) -> str:
    """Build the default CSV export file name."""
    return f"Shenron2_limits_{region}_{timestamp_for_filename()}.csv"


def print_table(rows: List[List[Any]]) -> None:
    """Print a bordered table."""
    if not rows:
        print("No rows found.")
        return

    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    print(sep)
    print("| " + " | ".join(str(rows[0][i]).ljust(widths[i]) for i in range(len(widths))) + " |")
    print(sep)

    for row in rows[1:]:
        print("| " + " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(widths))) + " |")

    print(sep)


def print_limit_tables(rows: List[Dict[str, Any]]) -> None:
    """Print collected limits grouped by service."""
    services = sorted({str(row["service"]) for row in rows})

    for service in services:
        display_names = {
            str(row.get("service_display_name") or row["service"])
            for row in rows
            if row["service"] == service
        }
        display_name = sorted(display_names)[0] if display_names else service
        heading = f"{display_name} ({service})" if display_name != service else service
        print(f"\n===== {heading} =====")
        table_rows: List[List[Any]] = [
            ["AD", "LimitName", "Scope", "Usage", "Available", "ServiceLimit"]
        ]

        for row in [item for item in rows if item["service"] == service]:
            table_rows.append(
                [
                    row["ad"],
                    row["limit_name"],
                    row["scope_type"],
                    row["usage"],
                    row["available"],
                    row["service_limit"],
                ]
            )

        print_table(table_rows)


def get_availability(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    service: str,
    limit_name: str,
    ad: str,
) -> Tuple[Any, Any, Optional[str]]:
    """Get usage and availability, distinguishing unsupported limits from failures."""
    args = [
        "limits", "resource-availability", "get",
        "--compartment-id", tenancy_id,
        "--service-name", service,
        "--limit-name", limit_name,
        "--region", region,
    ]

    if ad and ad != "None":
        args += ["--availability-domain", ad]

    cmd = build_oci_cmd(oci_bin, args + ["--output", "json"], profile, config_file)
    proc = run_cmd(cmd, allow_fail=True)

    if proc.returncode != 0:
        error_text = " ".join(proc.stderr.strip().split())
        error_lower = error_text.lower()
        unsupported_markers = (
            '"status": 404',
            "'status': 404",
            "status: 404",
            "status code: 404",
            "notauthorizedornotfound",
        )
        if any(marker in error_lower for marker in unsupported_markers):
            return "-", "-", None

        detail = error_text[:240] or "no error details returned"
        return "-", "-", f"Availability lookup failed (OCI CLI exit {proc.returncode}): {detail}"

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "-", "-", "Availability lookup failed: OCI CLI returned invalid JSON."

    if not data or "data" not in data:
        return "-", "-", "Availability lookup failed: OCI response did not include data."

    used = data["data"].get("used", "-")
    available = data["data"].get("available", "-")

    return (
        used if used is not None else "-",
        available if available is not None else "-",
        None,
    )


def is_curated_limit(service: str, limit_name: str) -> bool:
    """Return True when a limit is in the original focused OKE list."""
    return limit_name in CURATED_LIMITS.get(service, [])


def collect_limits(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    services: List[Dict[str, str]],
    curated_only: bool = False,
    show_tables: bool = True,
    show_progress: bool = True,
) -> List[Dict[str, Any]]:
    """Collect limits for the requested services."""
    all_rows: List[Dict[str, Any]] = []
    service_failures: List[str] = []
    availability_failures: List[str] = []
    total_services = len(services)

    for service_index, service_entry in enumerate(services, start=1):
        service = service_entry["name"]
        service_display_name = service_entry.get("display_name") or service
        service_label = (
            f"{service_display_name} ({service})"
            if service_display_name != service
            else service
        )
        progress(f"\n[{service_index}/{total_services}] Listing limits for {service_label}...", show_progress)
        data = run_json(
            oci_bin,
            [
                "limits", "value", "list",
                "--compartment-id", tenancy_id,
                "--service-name", service,
                "--all",
                "--region", region,
            ],
            profile,
            config_file,
            allow_fail=True,
            warning_context=f"Could not list limits for service '{service}'.",
        )

        if data is None:
            service_failures.append(service_label)

        selected_items: List[Dict[str, Any]] = []
        for item in extract_items(data):
            name = item.get("name")
            if not name:
                continue

            if curated_only and not is_curated_limit(service, str(name)):
                continue

            selected_items.append(item)

        if not selected_items:
            progress(f"    No matching limits returned for {service_label}.", show_progress)
            continue

        progress(
            f"    Found {len(selected_items)} limit rows. Checking usage and availability...",
            show_progress,
        )

        total_limits = len(selected_items)
        for limit_index, item in enumerate(selected_items, start=1):
            name = item.get("name")
            ad = item.get("availability-domain") or item.get("availabilityDomain") or "None"
            scope_type = item.get("scope-type") or item.get("scopeType") or "-"
            service_limit = item.get("value", "-")
            if show_progress and (limit_index == 1 or limit_index % 10 == 0 or limit_index == total_limits):
                print(
                    f"    [{limit_index}/{total_limits}] Availability: {name} / AD={ad}",
                    flush=True,
                )
            used, available, availability_error = get_availability(
                oci_bin,
                profile,
                config_file,
                tenancy_id,
                region,
                service,
                str(name),
                str(ad),
            )
            if availability_error:
                availability_failures.append(f"{service}/{name}/AD={ad}: {availability_error}")

            all_rows.append(
                {
                    "service": service,
                    "service_display_name": service_display_name,
                    "ad": str(ad),
                    "limit_name": str(name),
                    "scope_type": scope_type,
                    "usage": used,
                    "available": available,
                    "service_limit": service_limit,
                    "collection_note": availability_error or "",
                }
            )

    all_rows.sort(
        key=lambda row: (
            str(row.get("service_display_name") or row["service"]),
            str(row["service"]),
            str(row["limit_name"]),
            str(row["ad"]),
        )
    )

    if service_failures or availability_failures:
        print("\nWARNING: Limit collection completed with partial OCI data.")
        if service_failures:
            print(f"  Failed to list limits for {len(service_failures)} service(s):")
            for service_label in service_failures[:10]:
                print(f"    - {service_label}")
            if len(service_failures) > 10:
                print(f"    - ... and {len(service_failures) - 10} more")
        if availability_failures:
            print(f"  Availability lookup failed for {len(availability_failures)} limit row(s).")
            for failure in availability_failures[:5]:
                print(f"    - {failure}")
            if len(availability_failures) > 5:
                print(f"    - ... and {len(availability_failures) - 5} more")
            print("  Affected exported rows are marked in the CSV notes column.")

    if show_tables:
        print_limit_tables(all_rows)

    return all_rows


def csv_export_row(
    row: Dict[str, Any],
    tenancy_id: str,
    region: str,
    generated_at_utc: str,
    row_number: int,
) -> Dict[str, Any]:
    """Convert an internal limit row to the customer-editable CSV shape."""
    return {
        "generated_at_utc": generated_at_utc,
        "tenancy_id": tenancy_id,
        "region": region,
        "service": row["service"],
        "service_display_name": row.get("service_display_name") or row["service"],
        "limit_name": row["limit_name"],
        "availability_domain": row["ad"],
        "scope_type": row["scope_type"],
        "usage": row["usage"],
        "available": row["available"],
        "current_service_limit": row["service_limit"],
        "requested_service_limit": "",
        "requested_increase_delta": f'=IF(L{row_number}="","",L{row_number}-K{row_number})',
        "apply": "no",
        "notes": row.get("collection_note", ""),
    }


def write_limits_csv(
    path: str,
    rows: List[Dict[str, Any]],
    tenancy_id: str,
    region: str,
) -> None:
    """Write the current limits to a CSV template."""
    generated_at_utc = datetime.now(timezone.utc).isoformat()

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row_number, row in enumerate(rows, start=2):
            writer.writerow(csv_export_row(row, tenancy_id, region, generated_at_utc, row_number))


def read_limits_csv(path: str) -> List[Dict[str, str]]:
    """Read an edited limits CSV and validate the expected columns."""
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        missing = [column for column in REQUIRED_APPLY_COLUMNS if column not in fieldnames]
        if missing:
            print("ERROR: CSV is missing required columns:")
            for column in missing:
                print(f"  - {column}")
            sys.exit(1)

        return [dict(row) for row in reader]


def ask_yes_no(prompt: str, default: str = "n") -> bool:
    """Prompt y/n."""
    suffix = " [y/N]: " if default.lower() == "n" else " [Y/n]: "

    while True:
        value = input(prompt + suffix).strip().lower()

        if not value:
            return default.lower() == "y"

        if value in ["y", "yes"]:
            return True

        if value in ["n", "no"]:
            return False

        print("Please answer y or n.")


def ask_menu_choice() -> str:
    """Ask the user which workflow to run."""
    print("\nWhat would you like to do?")
    print("  1. Export current limits to CSV")
    print("  2. Apply limit increases")
    print("  3. Exit")

    while True:
        choice = input("Select option [1/2/3]: ").strip()
        if choice in {"1", "2", "3"}:
            return choice
        print("Please select 1, 2, or 3.")


def ask_export_scope() -> str:
    """Ask whether export should scan all services or selected services."""
    print("\nExport current limits:")
    print("  1. Scan and pull all current tenant limits")
    print("  2. Select specific services")

    while True:
        choice = input("Select export option [1/2]: ").strip()
        if choice == "1":
            return "all"
        if choice == "2":
            return "select"
        print("Please select 1 or 2.")


def ask_apply_method() -> str:
    """Ask how the user wants to provide new limit values."""
    print("\nApply limit increases:")
    print("  1. Import requested limits from edited CSV")
    print("  2. Enter requested limits in this terminal")

    while True:
        choice = input("Select apply option [1/2]: ").strip()
        if choice == "1":
            return "csv"
        if choice == "2":
            return "terminal"
        print("Please select 1 or 2.")


def parse_service_selection(selection: str, max_index: int) -> List[int]:
    """Parse service numbers such as '24,29,34' or '24-30'."""
    selected = set()
    tokens = selection.replace(",", " ").split()

    for token in tokens:
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.strip() or not end_text.strip():
                raise ValueError(f"Invalid range: {token}")
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"Invalid descending range: {token}")
            for index in range(start, end + 1):
                selected.add(index)
        else:
            selected.add(int(token))

    invalid = [index for index in selected if index < 1 or index > max_index]
    if invalid:
        invalid_text = ", ".join(str(index) for index in sorted(invalid))
        raise ValueError(f"Selection out of range: {invalid_text}")

    return sorted(selected)


def select_services_interactively(services: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Let the user select discovered services by number."""
    print("\nSelect services:")
    print_service_list(services)
    print("\nEnter service numbers separated by commas, for example: 24,29,34")
    print("Ranges are also supported, for example: 24-30")

    while True:
        selection = input("Service numbers: ").strip()
        if not selection:
            print("Please enter at least one service number.")
            continue

        try:
            selected_indexes = parse_service_selection(selection, len(services))
        except ValueError as exc:
            print(f"Invalid selection: {exc}")
            continue

        selected_services = [services[index - 1] for index in selected_indexes]
        print("\nSelected services:")
        print_service_list(selected_services)

        if ask_yes_no("Use these services?", default="y"):
            return selected_services


def parse_number(value: str) -> Any:
    """Parse a user-entered number, preserving integers where possible."""
    value = value.strip()
    if "." in value or "e" in value.lower():
        return float(value)

    return int(value)


def ask_positive_number(prompt: str, greater_than: Optional[float] = None) -> Any:
    """Ask for a positive numeric value, optionally above a current limit."""
    while True:
        value = input(prompt).strip()
        if not value:
            print("Value cannot be empty.")
            continue

        try:
            parsed = parse_number(value)
        except ValueError:
            print("Please enter a number only.")
            continue

        if parsed <= 0:
            print("Please enter a positive number.")
            continue

        if greater_than is not None and float(parsed) <= greater_than:
            print(f"Requested value must be greater than the current service limit ({greater_than:g}).")
            continue

        return parsed


def numeric_value(value: Any) -> Optional[float]:
    """Convert a value to float when it is numeric enough to compare."""
    try:
        text = str(value).strip()
        if not text or text == "-":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def numbers_equal(left: Optional[float], right: Optional[float]) -> bool:
    """Compare numeric values with a tiny tolerance."""
    if left is None or right is None:
        return left is right
    return abs(left - right) < 0.000001


def is_apply_enabled(value: str) -> bool:
    """Return True when the CSV apply column explicitly asks to apply a row."""
    return value.strip().lower() in APPLY_TRUE_VALUES


def csv_rows_marked_for_apply(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Return only CSV rows explicitly selected for submission."""
    return [row for row in rows if is_apply_enabled(row.get("apply", ""))]


def validate_csv_context(
    rows: List[Dict[str, str]],
    tenancy_id: str,
    region: str,
) -> None:
    """Stop apply mode when selected CSV rows target another OCI context."""
    errors: List[str] = []

    for line_number, row in enumerate(rows, start=2):
        if not is_apply_enabled(row.get("apply", "")):
            continue

        csv_tenancy = row.get("tenancy_id", "").strip()
        csv_region = row.get("region", "").strip()

        if not csv_tenancy:
            errors.append(f"Line {line_number}: tenancy_id is empty.")
        elif csv_tenancy != tenancy_id:
            errors.append(
                f"Line {line_number}: CSV tenancy_id does not match the active tenancy."
            )

        if not csv_region:
            errors.append(f"Line {line_number}: region is empty.")
        elif csv_region.lower() != region.lower():
            errors.append(
                f"Line {line_number}: CSV region '{csv_region}' does not match active region '{region}'."
            )

    if not errors:
        return

    print("\nERROR: The edited CSV does not match the active OCI context.")
    print(f"Active tenancy OCID: {tenancy_id}")
    print(f"Active region: {region}")
    for error in errors[:10]:
        print(f"  - {error}")
    if len(errors) > 10:
        print(f"  - ... and {len(errors) - 10} more")
    print("No limit increase request was submitted.")
    sys.exit(1)


def row_key(service: str, limit_name: str, ad: str) -> Tuple[str, str, str]:
    """Build a stable key for matching CSV rows to live OCI rows."""
    return service.strip(), limit_name.strip(), (ad.strip() or "None")


def limit_rows_by_key(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Index live limit rows by service, limit name and AD."""
    return {
        row_key(str(row["service"]), str(row["limit_name"]), str(row["ad"])): row
        for row in rows
    }


def services_from_csv(rows: List[Dict[str, str]], fallback_services: List[str]) -> List[str]:
    """Return services that must be refreshed for apply mode."""
    services = sorted({
        row.get("service", "").strip()
        for row in rows
        if row.get("service", "").strip()
    })

    return services or fallback_services


def build_apply_plan(
    csv_rows: List[Dict[str, str]],
    live_rows: List[Dict[str, Any]],
    region: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Build planned limit increase request items from an edited CSV."""
    live_by_key = limit_rows_by_key(live_rows)
    plan: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for line_number, csv_row in enumerate(csv_rows, start=2):
        if not is_apply_enabled(csv_row.get("apply", "")):
            if csv_row.get("requested_service_limit", "").strip():
                warnings.append(
                    f"Line {line_number}: requested_service_limit is filled but apply is not yes. Skipped."
                )
            continue

        requested_text = csv_row.get("requested_service_limit", "").strip()
        if not requested_text:
            warnings.append(f"Line {line_number}: apply=yes but requested_service_limit is empty. Skipped.")
            continue

        try:
            requested_value = parse_number(requested_text)
        except ValueError:
            warnings.append(f"Line {line_number}: requested_service_limit is not numeric. Skipped.")
            continue

        if requested_value <= 0:
            warnings.append(f"Line {line_number}: requested_service_limit must be positive. Skipped.")
            continue

        service = csv_row.get("service", "").strip()
        limit_name = csv_row.get("limit_name", "").strip()
        ad = csv_row.get("availability_domain", "").strip() or "None"

        live_row = live_by_key.get(row_key(service, limit_name, ad))
        if not live_row:
            warnings.append(
                f"Line {line_number}: {service}/{limit_name}/{ad} was not found in the live OCI refresh. Skipped."
            )
            continue

        live_current = numeric_value(live_row["service_limit"])
        csv_current = numeric_value(csv_row.get("current_service_limit", ""))

        if live_current is None:
            warnings.append(
                f"Line {line_number}: live current_service_limit is not numeric for {service}/{limit_name}. Skipped."
            )
            continue

        if not numbers_equal(csv_current, live_current):
            warnings.append(
                "Line "
                f"{line_number}: CSV current limit for {service}/{limit_name}/{ad} is stale "
                f"(CSV={csv_row.get('current_service_limit')}, live={live_row['service_limit']}). "
                "Using the live value for validation."
            )

        if float(requested_value) <= live_current:
            warnings.append(
                f"Line {line_number}: requested value {requested_value} is not greater than live current "
                f"limit {live_current:g}. Skipped."
            )
            continue

        request_item: Dict[str, Any] = {
            "serviceName": service,
            "limitName": limit_name,
            "region": region,
            "value": requested_value,
        }

        if ad != "None":
            request_item["scope"] = ad

        plan.append(
            {
                "line_number": line_number,
                "service": service,
                "limit_name": limit_name,
                "ad": ad,
                "current": live_current,
                "requested": requested_value,
                "delta": float(requested_value) - live_current,
                "request_item": request_item,
            }
        )

    return plan, warnings


def request_item_from_limit_row(
    row: Dict[str, Any],
    requested_value: Any,
    region: str,
) -> Dict[str, Any]:
    """Build an OCI limits-increase request item from a live limit row."""
    request_item: Dict[str, Any] = {
        "serviceName": row["service"],
        "limitName": row["limit_name"],
        "region": region,
        "value": requested_value,
    }

    if row["ad"] != "None":
        request_item["scope"] = row["ad"]

    return request_item


def ask_limit_action(row: Dict[str, Any]) -> str:
    """Ask whether to skip or increase one limit row."""
    current = row["service_limit"]
    usage = row["usage"]
    available = row["available"]
    ad = row["ad"]
    limit_name = row["limit_name"]
    service_name = row.get("service_display_name") or row["service"]

    while True:
        value = input(
            f"[{service_name}] {limit_name} / AD={ad} / Usage={usage} / Available={available} / "
            f"Current={current}: [s]kip/[i]ncrease? "
        ).strip().lower()

        if not value or value in {"s", "skip"}:
            return "skip"

        if value in {"i", "increase"}:
            return "increase"

        print("Please enter s to skip or i to increase.")


def build_terminal_apply_plan(
    live_rows: List[Dict[str, Any]],
    region: str,
) -> List[Dict[str, Any]]:
    """Interactively build planned limit increases from live OCI rows."""
    plan: List[Dict[str, Any]] = []

    print("\nReview each limit. Press Enter to skip, or type i to enter a new requested limit.")

    for row_number, row in enumerate(live_rows, start=1):
        action = ask_limit_action(row)
        if action == "skip":
            continue

        current_limit = numeric_value(row["service_limit"])
        requested_value = ask_positive_number(
            "Requested new service limit value: ",
            greater_than=current_limit,
        )
        current_for_plan = current_limit if current_limit is not None else 0.0

        plan.append(
            {
                "line_number": row_number,
                "service": row["service"],
                "limit_name": row["limit_name"],
                "ad": row["ad"],
                "current": current_for_plan,
                "requested": requested_value,
                "delta": float(requested_value) - current_for_plan,
                "request_item": request_item_from_limit_row(row, requested_value, region),
            }
        )
        print("Added.\n")

    return plan


def print_apply_plan(plan: List[Dict[str, Any]]) -> None:
    """Print the final apply plan before submission."""
    table_rows: List[List[Any]] = [
        ["Item", "Service", "LimitName", "AD", "Current", "Requested", "Delta"]
    ]

    for item in plan:
        table_rows.append(
            [
                item["line_number"],
                item["service"],
                item["limit_name"],
                item["ad"],
                f"{item['current']:g}",
                item["requested"],
                f"{item['delta']:g}",
            ]
        )

    print("\nPlanned limit increase request items:")
    print_table(table_rows)


def get_limit_questions(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    service: str,
    limit_name: str,
) -> List[Dict[str, Any]]:
    """Get service-limit questionnaire questions."""
    payload = run_json(
        oci_bin,
        [
            "limits-increase", "question", "list",
            "--compartment-id", tenancy_id,
            "--service-name", service,
            "--limit-name", limit_name,
            "--all",
            "--region", region,
        ],
        profile,
        config_file,
        allow_fail=True,
    )

    questions = extract_items(payload)
    filtered = []

    for q in questions:
        q_limit = q.get("limit-name") or q.get("limitName") or ""
        if not q_limit or q_limit == limit_name:
            filtered.append(q)

    return filtered


def ask_questionnaire(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    service: str,
    limit_name: str,
) -> List[Dict[str, str]]:
    """Ask and collect questionnaire responses for selected limit."""
    questions = get_limit_questions(oci_bin, profile, config_file, tenancy_id, region, service, limit_name)

    if not questions and service == "load-balancer" and limit_name == "lb-flexible-bandwidth-sum":
        questions = [
            {
                "id": "expectedCapacityUsageNext60Days",
                "question-text": "What is your expected capacity usage for next 60 days?",
                "is-required": True,
            },
            {
                "id": "timelineForUsingEntireRequestedCapacity",
                "question-text": "What is the timeline for using the entire requested capacity?",
                "is-required": True,
            },
        ]

    if not questions:
        return []

    print(f"\nAdditional required details for {service} / {limit_name}:")
    responses: List[Dict[str, str]] = []

    for q in questions:
        qid = q.get("id")
        text = q.get("question-text") or q.get("questionText") or "Question"
        qtype = q.get("question-type") or q.get("questionType") or "TEXT"
        required = q.get("is-required") if "is-required" in q else q.get("isRequired", False)
        options = q.get("options") or {}

        print("")
        print(text)
        print(f"Type: {qtype} | Required: {required}")

        if options:
            print("Options:")
            for key, value in options.items():
                print(f"  {key}: {value}")

        while True:
            answer = input("Answer: ").strip()
            if answer or not required:
                break
            print("This field is required.")

        if answer and qid:
            responses.append({"id": str(qid), "questionResponse": answer})

    return responses


def add_questionnaires_to_items(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    items: List[Dict[str, Any]],
) -> None:
    """Add questionnaire responses to request items when OCI requires them."""
    for item in items:
        questionnaire = ask_questionnaire(
            oci_bin,
            profile,
            config_file,
            tenancy_id,
            region,
            item["serviceName"],
            item["limitName"],
        )
        if questionnaire:
            item["questionnaireResponse"] = questionnaire


def limits_increase_supported(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
) -> bool:
    """Return True when this OCI CLI has the limits-increase command group."""
    check = run_cmd(
        build_oci_cmd(oci_bin, ["limits-increase", "--help"], profile, config_file),
        allow_fail=True,
    )

    return check.returncode == 0


def install_latest_oci_cli() -> str:
    """Install or update a newer OCI CLI in ~/oci-cli-latest."""
    latest_python, latest_oci = latest_cli_paths()
    latest_dir = os.path.expanduser(LATEST_CLI_DIR)

    print("\nInstalling/updating latest OCI CLI in:")
    print(f"  {latest_dir}")
    print("This may take a few minutes in Cloud Shell.\n")

    if not os.path.exists(latest_python):
        run_streaming_cmd([sys.executable, "-m", "venv", latest_dir])

    run_streaming_cmd(
        [
            latest_python,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip<25",
            "setuptools",
            "wheel",
        ]
    )
    run_streaming_cmd([latest_python, "-m", "pip", "install", "--upgrade", "oci-cli"])

    if not os.path.exists(latest_oci):
        print(f"\nERROR: OCI CLI was not found after install: {latest_oci}")
        sys.exit(1)

    return latest_oci


def ensure_limits_increase_supported(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    auto_install: bool = False,
) -> str:
    """Ensure the selected OCI CLI can create service limit increase requests."""
    if limits_increase_supported(oci_bin, profile, config_file):
        return oci_bin

    print("\nThis OCI CLI does not support the required 'limits-increase' command group.")
    print(f"Current OCI CLI: {oci_bin}")

    _, latest_oci = latest_cli_paths()
    if os.path.exists(latest_oci):
        print(f"\nTrying newer OCI CLI found at: {latest_oci}")
        if limits_increase_supported(latest_oci, profile, config_file):
            print(f"Using OCI CLI: {latest_oci}")
            return latest_oci

    should_install = auto_install
    if not should_install:
        should_install = ask_yes_no(
            f"Install/update latest OCI CLI in {os.path.expanduser(LATEST_CLI_DIR)} now?",
            default="y",
        )

    if not should_install:
        print("\nCannot apply limit increases without a newer OCI CLI.")
        print("Manual install commands:")
        print("  python3 -m venv ~/oci-cli-latest")
        print("  ~/oci-cli-latest/bin/python -m pip install --upgrade \"pip<25\" setuptools wheel")
        print("  ~/oci-cli-latest/bin/python -m pip install --upgrade oci-cli")
        print("Then rerun:")
        print("  python3 Shenron2.py --mode apply --csv-file <your_csv_file>")
        sys.exit(1)

    installed_oci = install_latest_oci_cli()

    if not limits_increase_supported(installed_oci, profile, config_file):
        print("\nERROR: The installed OCI CLI still does not support 'limits-increase'.")
        print(f"Installed OCI CLI path: {installed_oci}")
        sys.exit(1)

    print(f"\nUsing newer OCI CLI for apply: {installed_oci}")
    return installed_oci


def file_uri(path: str) -> str:
    """Return a file URI accepted by OCI CLI file parameters."""
    return Path(path).resolve().as_uri()


def receipt_path_for(source_reference: str) -> str:
    """Build a human-readable receipt path."""
    if source_reference and (os.path.exists(source_reference) or os.path.dirname(source_reference)):
        base_dir = os.path.dirname(os.path.abspath(source_reference)) or os.getcwd()
    else:
        base_dir = os.getcwd()

    return os.path.join(base_dir, f"Shenron2_receipt_{timestamp_for_filename()}.txt")


def get_oci_cli_version(oci_bin: str) -> str:
    """Return OCI CLI version text when available."""
    version = run_cmd([oci_bin, "--version"], allow_fail=True)
    return version.stdout.strip() or "-"


def execution_context(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> Dict[str, str]:
    """Collect local and OCI identity context for the receipt."""
    config_profile = profile or os.environ.get("OCI_CLI_PROFILE", "DEFAULT")
    config_path = config_file or os.environ.get("OCI_CLI_CONFIG_FILE", "~/.oci/config")
    oci_user_ocid = read_oci_config_value("user", config_profile, config_path) or "-"

    return {
        "local_user": getpass.getuser(),
        "host": platform.node() or "-",
        "working_directory": os.getcwd(),
        "command": " ".join(sys.argv),
        "oci_cli": oci_bin,
        "oci_cli_version": get_oci_cli_version(oci_bin),
        "oci_profile": profile or os.environ.get("OCI_CLI_PROFILE", "DEFAULT"),
        "oci_config_file": os.path.expanduser(config_file or os.environ.get("OCI_CLI_CONFIG_FILE", "~/.oci/config")),
        "oci_auth": os.environ.get("OCI_CLI_AUTH", "config_file"),
        "oci_user_ocid": oci_user_ocid,
        "tenancy_id": tenancy_id,
        "region": region,
    }


def format_receipt_item(index: int, item: Dict[str, Any]) -> List[str]:
    """Format one submitted limit item for the receipt."""
    lines = [
        f"{index}. Service: {item.get('serviceName', '-')}",
        f"   LimitName: {item.get('limitName', '-')}",
        f"   Region: {item.get('region', '-')}",
        f"   Requested service limit: {item.get('value', '-')}",
    ]

    if item.get("scope"):
        lines.append(f"   Scope / AD: {item['scope']}")
    else:
        lines.append("   Scope / AD: None")

    return lines


def write_receipt(
    path: str,
    context: Dict[str, str],
    source_kind: str,
    source_reference: str,
    display_name: str,
    justification: str,
    items: List[Dict[str, Any]],
    returncode: int,
    request: Optional[Dict[str, Any]],
    stdout: str,
    stderr: str,
    items_file: Optional[str] = None,
) -> None:
    """Write a human-readable execution receipt."""
    request_id = request.get("RequestId", "-") if request else "-"
    state = request.get("State", "-") if request else "-"
    created = request.get("Created", "-") if request else "-"

    lines = [
        "Shenron2 OCI Service Limit Request Receipt",
        "=" * 40,
        f"Receipt created at UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Result: {'SUCCESS' if returncode == 0 else 'FAILED'}",
        "",
        "Created Service Limit Request",
        "-" * 40,
        f"Request OCID: {request_id}",
        f"State: {state}",
        f"OCI time created: {created}",
        f"Display name: {display_name}",
        f"Request reason: {justification}",
        "",
        "Execution Identity",
        "-" * 40,
        f"Local user: {context['local_user']}",
        f"Host: {context['host']}",
        f"Working directory: {context['working_directory']}",
        f"Command: {context['command']}",
        "",
        "OCI Context",
        "-" * 40,
        f"Tenancy OCID: {context['tenancy_id']}",
        f"Region: {context['region']}",
        f"OCI user OCID: {context['oci_user_ocid']}",
        f"OCI profile: {context['oci_profile']}",
        f"OCI auth: {context['oci_auth']}",
        f"OCI config file: {context['oci_config_file']}",
        f"OCI CLI: {context['oci_cli']}",
        f"OCI CLI version: {context['oci_cli_version']}",
        "",
        "Input Source",
        "-" * 40,
        f"Source type: {source_kind}",
        f"Source reference: {source_reference}",
        "",
        "Submitted Limits",
        "-" * 40,
    ]

    for index, item in enumerate(items, start=1):
        lines.extend(format_receipt_item(index, item))
        lines.append("")

    lines.extend(
        [
            "OCI CLI Output",
            "-" * 40,
            "stdout:",
            stdout.strip() or "-",
            "",
            "stderr:",
            stderr.strip() or "-",
        ]
    )

    if items_file:
        lines.extend(["", f"Request items JSON retained at: {items_file}"])

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def create_limit_request(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
    items: List[Dict[str, Any]],
    source_reference: str,
    source_kind: str = "CSV",
) -> None:
    """Create the OCI limit increase request."""
    display_name_default = f"OCI onboarding service limit increase from {source_kind} - {region}"
    justification_default = (
        "As part of an OCI onboarding engagement guided by our OCI Cloud Adoption Manager, "
        "we have reviewed the planned workload requirements and identified the attached service "
        "limit increases as necessary to support the expected deployment, scaling, and operational "
        "readiness outcomes in this tenancy and region."
    )

    display_name = input(f"\nDisplay name [{display_name_default}]: ").strip() or display_name_default
    justification = input(f"Request reason shown in OCI [{justification_default}]: ").strip() or justification_default

    timestamp = int(datetime.now(timezone.utc).timestamp())
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        prefix=f"oci_limit_items_{timestamp}_",
        suffix=".json",
    ) as f:
        items_file = f.name
        json.dump(items, f)

    cmd = build_oci_cmd(
        oci_bin,
        [
            "limits-increase", "limits-increase-request", "create",
            "--region", region,
            "--compartment-id", tenancy_id,
            "--display-name", display_name,
            "--justification", justification,
            "--items", file_uri(items_file),
            "--query", 'data.{RequestId:id,State:"lifecycle-state",DisplayName:"display-name",Created:"time-created"}',
            "--output", "json",
        ],
        profile,
        config_file,
    )

    print("\nCreating service limit request...\n")
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    receipt_path = receipt_path_for(source_reference or os.getcwd())
    context = execution_context(oci_bin, profile, config_file, tenancy_id, region)
    request: Optional[Dict[str, Any]] = None

    if proc.returncode == 0:
        try:
            request = json.loads(proc.stdout)
        except json.JSONDecodeError:
            request = None

        write_receipt(
            receipt_path,
            context,
            source_kind,
            source_reference,
            display_name,
            justification,
            items,
            proc.returncode,
            request,
            proc.stdout,
            proc.stderr,
        )

        request_id = None
        if request:
            print(json.dumps(request, indent=2))
            request_id = request.get("RequestId")
            if request_id:
                print(f"\nRequestId: {cyan(str(request_id))}")
        elif proc.stdout.strip():
            print(proc.stdout.strip())

        if request_id:
            print("\nDone. The RequestId above is the customer-visible limit increase request ID.")
        else:
            print("\nWARNING: OCI CLI returned success, but Shenron2 could not parse a RequestId.")
            print("Verify the request in the OCI Console and review the receipt before retrying.")
        print(f"Receipt saved to: {receipt_path}")
        print("This TXT receipt records who executed the action, the OCI context, and the created request OCID.")

        try:
            os.remove(items_file)
        except OSError:
            pass
    else:
        write_receipt(
            receipt_path,
            context,
            source_kind,
            source_reference,
            display_name,
            justification,
            items,
            proc.returncode,
            request,
            proc.stdout,
            proc.stderr,
            items_file=items_file,
        )

        print("\nRequest creation failed.")
        if proc.stderr.strip():
            print(proc.stderr.strip())
        print(f"Items JSON was saved here: {items_file}")
        print(f"Failure receipt saved to: {receipt_path}")
        sys.exit(proc.returncode or 1)


def run_export_workflow(
    args: argparse.Namespace,
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> None:
    """Export current limits to a CSV file."""
    csv_path = args.csv_file or default_export_path(region)
    show_progress = not args.no_progress

    if args.services:
        services = service_entries_from_names(parse_services(args.services))
        mode_description = "explicit --services list"
    elif args.curated_only:
        services = service_entries_from_names(DEFAULT_SERVICES)
        mode_description = "original curated OKE limit list only"
    else:
        export_scope = args.export_scope
        if not export_scope and getattr(args, "from_menu", False):
            export_scope = ask_export_scope()
        if not export_scope:
            export_scope = "all"

        discovered_services = discover_limit_services(
            oci_bin,
            profile,
            config_file,
            tenancy_id,
            region,
            show_progress=show_progress,
            list_services=export_scope == "all",
        )

        if export_scope == "select":
            services = select_services_interactively(discovered_services)
            mode_description = "selected OCI limit services"
        else:
            services = discovered_services
            mode_description = "all OCI limit services discovered from the tenancy/region"

    print("\nExporting current limits.")
    print(f"Mode: {mode_description}")
    print(f"Services selected: {len(services)}")

    rows = collect_limits(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        services,
        curated_only=args.curated_only,
        show_tables=not args.no_tables,
        show_progress=show_progress,
    )

    if not rows:
        print("\nNo limits were collected. CSV was not created.")
        return

    write_limits_csv(csv_path, rows, tenancy_id, region)

    print(f"\nCSV created: {os.path.abspath(csv_path)}")
    print("Edit requested_service_limit and set apply=yes for rows that should be submitted.")


def run_apply_csv_workflow(
    args: argparse.Namespace,
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> None:
    """Apply increases from an edited CSV file."""
    csv_path = args.csv_file
    if not csv_path:
        csv_path = input("Path to edited CSV file: ").strip()

    if not csv_path:
        print("ERROR: CSV path is required.")
        sys.exit(1)

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV file was not found: {csv_path}")
        sys.exit(1)

    csv_rows = read_limits_csv(csv_path)
    selected_csv_rows = csv_rows_marked_for_apply(csv_rows)
    if not selected_csv_rows:
        print("\nNo CSV rows have apply=yes. Nothing was submitted.")
        return

    validate_csv_context(csv_rows, tenancy_id, region)

    oci_bin = ensure_limits_increase_supported(
        oci_bin,
        profile,
        config_file,
        auto_install=args.install_latest_cli,
    )

    service_names = (
        parse_services(args.services)
        if args.services
        else services_from_csv(selected_csv_rows, [])
    )
    if not service_names:
        print("ERROR: Selected CSV rows do not contain a service name.")
        sys.exit(1)

    services = service_entries_from_names(service_names)
    show_progress = not args.no_progress

    print("\nRefreshing live OCI limits before applying changes.")
    print(f"Services selected: {len(services)}")
    if show_progress:
        print(f"Services: {', '.join(service_names)}", flush=True)

    live_rows = collect_limits(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        services,
        curated_only=False,
        show_tables=False,
        show_progress=show_progress,
    )

    plan, warnings = build_apply_plan(csv_rows, live_rows, region)

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if not plan:
        print("\nNo valid increases were found. Nothing was submitted.")
        return

    print_apply_plan(plan)

    if not ask_yes_no("\nSubmit these service limit increases now?", default="n"):
        print("Canceled. No request was created.")
        return

    request_items = [item["request_item"] for item in plan]
    add_questionnaires_to_items(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        request_items,
    )

    create_limit_request(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        request_items,
        csv_path,
        "CSV",
    )


def run_apply_terminal_workflow(
    args: argparse.Namespace,
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> None:
    """Apply increases entered interactively in the terminal."""
    oci_bin = ensure_limits_increase_supported(
        oci_bin,
        profile,
        config_file,
        auto_install=args.install_latest_cli,
    )

    show_progress = not args.no_progress

    if args.services:
        services = service_entries_from_names(parse_services(args.services))
    else:
        discovered_services = discover_limit_services(
            oci_bin,
            profile,
            config_file,
            tenancy_id,
            region,
            show_progress=show_progress,
            list_services=False,
        )
        services = select_services_interactively(discovered_services)

    print("\nRefreshing live OCI limits for selected services.")
    print(f"Services selected: {len(services)}")

    live_rows = collect_limits(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        services,
        curated_only=False,
        show_tables=True,
        show_progress=show_progress,
    )

    if not live_rows:
        print("\nNo limits were collected. Nothing was submitted.")
        return

    plan = build_terminal_apply_plan(live_rows, region)

    if not plan:
        print("\nNo limits selected for increase. Nothing was submitted.")
        return

    print_apply_plan(plan)

    if not ask_yes_no("\nSubmit these service limit increases now?", default="n"):
        print("Canceled. No request was created.")
        return

    request_items = [item["request_item"] for item in plan]
    add_questionnaires_to_items(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        request_items,
    )

    create_limit_request(
        oci_bin,
        profile,
        config_file,
        tenancy_id,
        region,
        request_items,
        "interactive terminal entry",
        "terminal",
    )


def run_apply_workflow(
    args: argparse.Namespace,
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> None:
    """Apply increases from CSV or guided terminal entry."""
    apply_method = args.apply_method
    if not apply_method and getattr(args, "from_menu", False):
        apply_method = ask_apply_method()
    if not apply_method:
        apply_method = "csv"

    if apply_method == "terminal":
        run_apply_terminal_workflow(args, oci_bin, profile, config_file, tenancy_id, region)
    else:
        run_apply_csv_workflow(args, oci_bin, profile, config_file, tenancy_id, region)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OCI service limit exporter and limit increase request submitter."
    )
    parser.add_argument("--region", help="OCI region, for example eu-frankfurt-1")
    parser.add_argument("--tenancy-id", help="Tenancy OCID")
    parser.add_argument("--profile", help="OCI CLI profile name. Default: OCI_CLI_PROFILE or DEFAULT")
    parser.add_argument("--config-file", help="OCI CLI config file. Default: OCI_CLI_CONFIG_FILE or ~/.oci/config")
    parser.add_argument("--oci-bin", help="Path to OCI CLI binary")
    parser.add_argument(
        "--mode",
        choices=["export", "apply"],
        help="Run without the main menu. Use export to create CSV or apply to submit limit increases.",
    )
    parser.add_argument(
        "--csv-file",
        help="CSV path. In export mode this is the output file. In apply mode this is the edited input file.",
    )
    parser.add_argument(
        "--apply-method",
        choices=["csv", "terminal"],
        help="Apply requested limit increases from an edited CSV or by entering values in the terminal.",
    )
    parser.add_argument(
        "--export-scope",
        choices=["all", "select"],
        help="Export all discovered services or select services by number after discovery.",
    )
    parser.add_argument(
        "--services",
        help="Comma-separated OCI service API names. Default export behavior discovers all OCI limit services.",
    )
    parser.add_argument(
        "--curated-only",
        action="store_true",
        help="Use only the original focused OKE service/limit list instead of discovering all services.",
    )
    parser.add_argument(
        "--no-tables",
        action="store_true",
        help="Do not print per-service tables during export.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Do not print progress while discovering services and collecting limit availability.",
    )
    parser.add_argument(
        "--install-latest-cli",
        action="store_true",
        help="In apply mode, install/update ~/oci-cli-latest automatically if the current OCI CLI lacks limits-increase.",
    )
    return parser.parse_args()


def print_runtime_context(
    oci_bin: str,
    profile: Optional[str],
    config_file: Optional[str],
    tenancy_id: str,
    region: str,
) -> None:
    """Print the runtime context before running a workflow."""
    print(f"Using OCI CLI: {oci_bin}")
    version = run_cmd([oci_bin, "--version"], allow_fail=True)
    if version.stdout.strip():
        print(f"OCI CLI version: {version.stdout.strip()}")

    print(f"Tenancy OCID: {tenancy_id}")
    print(f"Region: {region}")
    if profile:
        print(f"OCI CLI profile: {profile}")
    if config_file:
        print(f"OCI CLI config file: {os.path.expanduser(config_file)}")


def main() -> None:
    args = parse_args()
    oci_bin = find_oci_binary(args.oci_bin)
    cli_profile, cli_config_file, config_profile, config_file = resolve_cli_context(args)
    tenancy_id, region = resolve_tenancy_and_region(args, config_profile, config_file)

    print_runtime_context(oci_bin, cli_profile, cli_config_file, tenancy_id, region)

    mode = args.mode
    args.from_menu = not bool(mode)
    if not mode:
        choice = ask_menu_choice()
        if choice == "1":
            mode = "export"
        elif choice == "2":
            mode = "apply"
        else:
            print("Exiting. No action taken.")
            return

    if mode == "export":
        run_export_workflow(args, oci_bin, cli_profile, cli_config_file, tenancy_id, region)
    elif mode == "apply":
        run_apply_workflow(args, oci_bin, cli_profile, cli_config_file, tenancy_id, region)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nExited by user. No further action taken.")
        sys.exit(130)
