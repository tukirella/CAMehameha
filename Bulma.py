#!/usr/bin/env python3
"""
bulma.py — OCI AD-to-AD Compute migration via Backup & Restore (keeps original instance)

Zero-prompt execution:
  python3 bulma.py -- <INSTANCE_OCID>
  python3 bulma.py -- <INSTANCE_OCID_1> <INSTANCE_OCID_2>

Why "--"?
- If you accidentally type something that starts with "-", "--" tells argparse "end of options".

What Bulma auto-discovers:
- Region (from instance OCID: ocid1.instance.oc1.<region>....)
- Compartment ID (from instance details)
- Source AD (from instance details)
- Boot volume + attached block volumes
- Subnet + NSGs (from primary/secondary VNICs)
- Optional classic LB membership (LBaaS) if --restore-lb

Safety:
- Stops the original instance (for consistent backups)
- Does NOT delete or modify the original instance beyond STOP
- Creates a NEW instance in a different AD
- Networking recreated with NEW private IP(s) (cannot preserve IP while original exists)

Notes:
- Handles classic Load Balancer (oci-load-balancer) backends (IP:port). Does NOT handle Network Load Balancer (NLB).
"""

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

import oci
from oci.pagination import list_call_get_all_results


# =============================================================================
# Progress / Visibility (0-100%)
# =============================================================================

def _ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")


def _bar(pct: int, width: int = 28) -> str:
    pct = max(0, min(100, pct))
    filled = int(round(width * (pct / 100.0)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


class StepProgress:
    def __init__(self, label: str, steps: List[Tuple[str, int]], overall_prefix: str = ""):
        self.label = label
        self.overall_prefix = overall_prefix
        self.steps = self._normalize(steps)
        self._idx = -1
        self._completed_weight = 0
        self._current_step_name = ""
        self._current_step_weight = 0

    @staticmethod
    def _normalize(steps: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
        s = [(n, int(w)) for n, w in steps if int(w) > 0]
        total = sum(w for _, w in s)
        if total <= 0:
            return [("Work", 100)]
        if total == 100:
            return s

        scaled: List[Tuple[str, int]] = []
        acc = 0
        for i, (n, w) in enumerate(s):
            if i == len(s) - 1:
                sw = 100 - acc
            else:
                sw = max(1, int(round((w / float(total)) * 100)))
                if acc + sw > 99:
                    sw = max(1, 99 - acc)
            acc += sw
            scaled.append((n, sw))

        drift = 100 - sum(w for _, w in scaled)
        if drift != 0:
            name, w = scaled[-1]
            scaled[-1] = (name, max(1, w + drift))
        return scaled

    def start_step(self, name: str) -> None:
        for i in range(self._idx + 1, len(self.steps)):
            if self.steps[i][0] == name:
                self._idx = i
                self._current_step_name = name
                self._current_step_weight = self.steps[i][1]
                self._print(self._completed_weight, 0, "START")
                return
        raise ValueError("Step '{}' not found".format(name))

    def update(self, step_frac: float, detail: str = "") -> None:
        step_frac = max(0.0, min(1.0, step_frac))
        step_pct = int(round(step_frac * 100))
        overall = int(round(self._completed_weight + (step_frac * self._current_step_weight)))
        self._print(overall, step_pct, detail)

    def complete_step(self, detail: str = "DONE") -> None:
        self._print(self._completed_weight + self._current_step_weight, 100, detail)
        self._completed_weight += self._current_step_weight
        self._current_step_weight = 0
        self._current_step_name = ""

    def _print(self, overall_pct: int, step_pct: int, detail: str) -> None:
        prefix = (self.overall_prefix + " ") if self.overall_prefix else ""
        line = (
            "{ts} | {prefix}{label} | "
            "Overall {op:>3}% {obar} | "
            "Step {sp:>3}% {sbar} | "
            "{step}".format(
                ts=_ts(),
                prefix=prefix,
                label=self.label,
                op=max(0, min(100, overall_pct)),
                obar=_bar(overall_pct, 28),
                sp=max(0, min(100, step_pct)),
                sbar=_bar(step_pct, 18),
                step=self._current_step_name
            )
        )
        if detail:
            line += " -- " + detail
        print(line, flush=True)


class MultiProgress:
    def __init__(self, total_instances: int):
        self.total = max(1, int(total_instances))

    def overall_prefix(self, idx_1based: int) -> str:
        return "[{}/{}]".format(idx_1based, self.total)


# =============================================================================
# CLI
# =============================================================================

def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("{} is not an integer".format(value))
    if parsed <= 0:
        raise argparse.ArgumentTypeError("{} must be greater than zero".format(value))
    return parsed


def parse_args():
    p = argparse.ArgumentParser(description="Bulma: AD-to-AD migration via backup/restore (keeps original instance).")
    p.add_argument("instance_ids", nargs="+", help="Instance OCID(s). Example: python3 bulma.py -- ocid1.instance...")
    p.add_argument("--dest-ad", default="", help="Optional destination AD name. If omitted, Bulma picks a different AD automatically.")
    p.add_argument("--shape", default="", help="Optional shape for NEW instance. If omitted, keeps original shape.")
    p.add_argument("--ssh-key", default="", help="Optional SSH public key to inject into metadata for NEW instance (Linux).")
    p.add_argument("--ssh-key-file", default="", help="Optional path to SSH public key file to inject into NEW instance metadata.")
    p.add_argument("--profile", default="DEFAULT", help="OCI config profile name (default: DEFAULT).")

    p.add_argument("--restore-lb", action="store_true", help="Detect classic LB membership and re-add new backend(s).")
    p.add_argument("--lb-disable-old", action="store_true", help="After adding new backend(s), set old backend(s) drain+offline.")
    p.add_argument("--lb-compartment-id", default="", help="Compartment OCID where LB(s) live (default: same as instance compartment).")

    p.add_argument("--dry-run", action="store_true", help="Discover and validate the migration plan, then exit without stopping or creating resources.")
    p.add_argument("--restart-source-on-failure", action="store_true", help="If Bulma stopped the source instance and a later step fails, try to start it again.")
    p.add_argument("--copy-tags", action="store_true", help="Copy source instance freeform/defined tags to the new instance. Bulma run tags are always added.")
    p.add_argument("--manifest", default="", help="Path for JSON run manifest. Default: ./bulma-run-<UTC timestamp>.json")
    p.add_argument("--debug", action="store_true", help="Print a Python traceback if the run fails.")

    p.add_argument("--wait", type=positive_int, default=3600, help="Max wait per resource state change (seconds). Default 3600.")
    p.add_argument("--poll", type=positive_int, default=10, help="Polling interval seconds. Default 10.")
    args = p.parse_args()
    if args.ssh_key and args.ssh_key_file:
        p.error("Use either --ssh-key or --ssh-key-file, not both.")
    if args.poll > args.wait:
        p.error("--poll cannot be greater than --wait.")
    return args


# =============================================================================
# Region inference (from OCID)
# =============================================================================

def infer_region_from_instance_ocid(instance_ocid: str) -> str:
    """
    ocid example:
      ocid1.instance.oc1.eu-frankfurt-1.<unique...>
    region id is the 4th token (index 3) when splitting by '.'
    """
    parts = instance_ocid.split(".")
    if len(parts) < 5:
        raise ValueError("Instance OCID format unexpected: {}".format(instance_ocid))
    # parts[2] is 'oc1' (realm key). parts[3] is region id.
    region = parts[3].strip()
    if not region:
        raise ValueError("Could not infer region from OCID: {}".format(instance_ocid))
    return region


# =============================================================================
# Auth / client setup
# =============================================================================

def _is_cloud_shell() -> bool:
    """Detect OCI Cloud Shell by the env vars it always sets."""
    return bool(os.environ.get("OCI_CS_USER_OCID") or os.environ.get("CLOUDSHELL_ID"))


def _align_config_region(config: Dict[str, Any], region_hint: str, label: str) -> Dict[str, Any]:
    """
    Bulma derives the target region from the instance OCID. Keep client endpoints
    aligned with that region even if ~/.oci/config points somewhere else.
    """
    existing = config.get("region")
    if existing and existing != region_hint:
        print(
            "{} | [Auth] WARNING: {} region '{}' differs from instance OCID region '{}'. "
            "Using inferred region.".format(_ts(), label, existing, region_hint),
            flush=True,
        )
    config["region"] = region_hint
    return config


def get_signer_and_config(region_hint: str, profile: str = "DEFAULT"):
    """
    Returns (config, signer) with region always set.

    Priority:
    1. OCI Cloud Shell delegation token  (detected via OCI_CS_USER_OCID env var)
    2. Resource Principals  (OCI Functions / dynamic group with Resource Principal policies)
    3. Instance Principals  (running directly on an OCI Compute instance)
    4. ~/.oci/config        (developer workstation / CI with API-key credentials)

    IMPORTANT: Each method is validated with a cheap list_regions() call BEFORE
    returning, so the caller gets a clear, actionable error message instead of a
    cryptic 401 far into the migration.
    """
    auth_errors: Dict[str, str] = {}

    # --- 1. OCI Cloud Shell (delegation token) ---
    if _is_cloud_shell():
        try:
            # Cloud Shell writes the delegation token to a well-known path and
            # exposes OCI_DELEGATION_TOKEN_FILE (or the default path below).
            token_path = os.environ.get(
                "OCI_DELEGATION_TOKEN_FILE",
                os.path.expanduser("~/.oci/token")  # Cloud Shell default
            )
            delegation_token = None
            if os.path.exists(token_path):
                with open(token_path, "r") as fh:
                    delegation_token = fh.read().strip()

            # Load the Cloud Shell config (~/.oci/config is pre-populated in Cloud Shell)
            config = oci.config.from_file(profile_name=profile)
            config = _align_config_region(config, region_hint, "Cloud Shell config")

            if delegation_token:
                signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(
                    delegation_token=delegation_token
                )
            else:
                # Fallback: Cloud Shell may expose credentials via config only
                signer = None

            _validate_auth(config, signer, "Cloud Shell delegation token")
            print("{} | [Auth] Using Cloud Shell delegation token (region={})".format(
                _ts(), config.get("region")), flush=True)
            return config, signer
        except Exception as e:
            auth_errors["Cloud Shell delegation token"] = str(e)
            # Don't fall through to Instance Principals in Cloud Shell —
            # that's what caused the 404. Jump straight to config file.
            try:
                config = oci.config.from_file(profile_name=profile)
                config = _align_config_region(config, region_hint, "Cloud Shell ~/.oci/config fallback")
                signer = None
                _validate_auth(config, signer, "Cloud Shell ~/.oci/config fallback")
                print("{} | [Auth] Cloud Shell: using ~/.oci/config (profile={}, region={})".format(
                    _ts(), profile, config.get("region")), flush=True)
                return config, signer
            except Exception as e2:
                auth_errors["Cloud Shell ~/.oci/config fallback"] = str(e2)
                _raise_all_auth_failed(auth_errors, region_hint, profile)

    # --- 2. Try Resource Principals ---
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        region = getattr(signer, "region", None) or region_hint
        config = {
            "region": region_hint,
            "tenancy": getattr(signer, "tenancy_id", None),
        }
        _validate_auth(config, signer, "Resource Principals")
        print("{} | [Auth] Using Resource Principals signer (region={})".format(_ts(), region_hint), flush=True)
        return config, signer
    except Exception as e:
        auth_errors["Resource Principals"] = str(e)

    # --- 3. Try Instance Principals ---
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        region = getattr(signer, "region", None) or region_hint
        config = {
            "region": region_hint,
            "tenancy": getattr(signer, "tenancy_id", None),
        }
        _validate_auth(config, signer, "Instance Principals")
        print("{} | [Auth] Using Instance Principals signer (region={})".format(_ts(), region_hint), flush=True)
        return config, signer
    except Exception as e:
        auth_errors["Instance Principals"] = str(e)

    # --- 3. Fall back to ~/.oci/config ---
    try:
        config = oci.config.from_file(profile_name=profile)
    except Exception as e:
        auth_errors["~/.oci/config (profile={})".format(profile)] = str(e)
        _raise_all_auth_failed(auth_errors, region_hint, profile)

    signer = None

    config = _align_config_region(config, region_hint, "~/.oci/config profile {}".format(profile))

    if not config.get("tenancy"):
        raise RuntimeError(
            "Missing 'tenancy' in ~/.oci/config (profile={}).\n"
            "Add:  tenancy = ocid1.tenancy.oc1..<unique_id>\n"
            "See:  https://docs.oracle.com/iaas/Content/API/Concepts/sdkconfig.htm".format(profile)
        )

    # Validate now, before any migration work starts
    try:
        _validate_auth(config, signer, "~/.oci/config (profile={})".format(profile))
    except RuntimeError:
        raise  # already has a clear message from _validate_auth / _raise_auth_service_error

    print("{} | [Auth] Using ~/.oci/config (profile={}, region={})".format(_ts(), profile, config.get("region")), flush=True)
    return config, signer


def _validate_auth(config: Dict[str, Any], signer, label: str) -> None:
    """
    Fire a cheap, read-only API call (list_regions) to confirm credentials work
    before any migration work begins.
    """
    try:
        kwargs = {"signer": signer} if signer else {}
        identity = oci.identity.IdentityClient(config, **kwargs)
        identity.list_regions()
    except oci.exceptions.ServiceError as e:
        _raise_auth_service_error(e, label)


def _raise_auth_service_error(e: oci.exceptions.ServiceError, label: str) -> None:
    """Translate a raw ServiceError into an actionable RuntimeError."""
    if e.status == 401:
        raise RuntimeError(
            "OCI authentication failed (401 NotAuthenticated) with {}.\n\n"
            "Common causes and fixes:\n"
            "  1. API key fingerprint mismatch\n"
            "     -> Re-upload your public key at: OCI Console > Profile > API Keys\n"
            "     -> Verify fingerprint matches: openssl pkey -in ~/.oci/oci_api_key.pem -pubout | openssl md5 -c\n"
            "  2. Wrong private key path in ~/.oci/config\n"
            "     -> key_file must be an absolute path, e.g.: key_file=/home/user/.oci/oci_api_key.pem\n"
            "     -> Check the file exists and is readable: ls -la <key_file path>\n"
            "  3. Incorrect user_ocid or tenancy_ocid in ~/.oci/config\n"
            "     -> Copy exact values from OCI Console > Profile\n"
            "  4. API key deleted or deactivated in OCI Console\n"
            "     -> Create a new key pair and upload the public key\n"
            "  5. System clock skew > 5 minutes\n"
            "     -> Check: date -u  (must be within 5 min of UTC)\n"
            "     -> Fix:   sudo chronyc makestep  or  sudo ntpdate pool.ntp.org\n\n"
            "Quick validation (requires OCI CLI):\n"
            "     oci iam region list\n\n"
            "Raw OCI error: {}".format(label, e)
        )
    if e.status == 403:
        raise RuntimeError(
            "OCI authorisation failed (403 Forbidden) with {}.\n"
            "Credentials are valid but the user/principal lacks the required IAM policy.\n"
            "Ensure your user/dynamic group has at minimum:\n"
            "  Allow group <your-group> to manage instance-family in compartment <compartment>\n"
            "  Allow group <your-group> to manage volume-family in compartment <compartment>\n"
            "  Allow group <your-group> to manage virtual-network-family in compartment <compartment>\n\n"
            "Raw OCI error: {}".format(label, e)
        )
    raise RuntimeError("OCI API error during auth validation ({}): {}".format(label, e))


def _raise_all_auth_failed(errors: Dict[str, str], region_hint: str, profile: str) -> None:
    lines = ["Authentication failed. All methods exhausted:\n"]
    for method, err in errors.items():
        lines.append("  {}: {}".format(method, err))
    lines += [
        "\nRemediation options:",
        "  a) Run this script on an OCI Compute instance that has an Instance Principal policy.",
        "  b) Create ~/.oci/config with valid API-key credentials:",
        "     https://docs.oracle.com/iaas/Content/API/Concepts/sdkconfig.htm",
        "  c) Use --profile <PROFILE_NAME> if your config uses a non-default profile.",
        "  d) Set OCI_CONFIG_FILE env var to point to your config file.",
        "     Inferred region from OCID: {}".format(region_hint),
    ]
    raise RuntimeError("\n".join(lines))


def get_tenancy_id(config: Dict[str, Any], signer) -> str:
    if config.get("tenancy"):
        return config["tenancy"]
    if signer is not None and getattr(signer, "tenancy_id", None):
        return signer.tenancy_id
    raise RuntimeError(
        "Tenancy OCID is missing.\n"
        "  - For ~/.oci/config: add 'tenancy = ocid1.tenancy.oc1..<unique_id>'\n"
        "  - For Instance/Resource Principals: ensure the dynamic group policy exposes tenancy."
    )


def get_clients(config: Dict[str, Any], signer=None):
    kwargs = {"signer": signer} if signer else {}
    identity = oci.identity.IdentityClient(config, **kwargs)
    compute = oci.core.ComputeClient(config, **kwargs)
    network = oci.core.VirtualNetworkClient(config, **kwargs)
    block = oci.core.BlockstorageClient(config, **kwargs)
    lb = oci.load_balancer.LoadBalancerClient(config, **kwargs)
    lb_ops = oci.load_balancer.LoadBalancerClientCompositeOperations(lb)
    return identity, compute, network, block, lb, lb_ops


def default_manifest_path() -> str:
    return "bulma-run-{}.json".format(datetime.utcnow().strftime("%Y%m%d%H%M%S"))


def write_manifest(path: str, manifest: Dict[str, Any]) -> None:
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    manifest["updated_at"] = _ts()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)


def read_ssh_key(args) -> str:
    if args.ssh_key:
        return args.ssh_key.strip()
    if not args.ssh_key_file:
        return ""
    with open(args.ssh_key_file, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def bulma_freeform_tags(run_id: str, source_instance_id: str) -> Dict[str, str]:
    return {
        "BulmaRunId": run_id,
        "BulmaSourceInstanceId": source_instance_id,
    }


def merge_freeform_tags(base: Optional[Dict[str, str]], extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    merged = dict(base or {})
    merged.update(extra or {})
    return merged


# =============================================================================
# Wait helper
# =============================================================================

def wait_for_state(getter_fn, desired_state: str, max_wait_seconds: int, interval: int, label: str,
                   progress: Optional["StepProgress"] = None):
    start = time.time()
    last_state = None
    while True:
        obj = getter_fn()
        state = getattr(obj, "lifecycle_state", None)

        if state != last_state:
            last_state = state
            if progress:
                progress.update(min(0.95, (time.time() - start) / float(max_wait_seconds)),
                                detail="{} state={} (waiting for {})".format(label, state, desired_state))

        if state == desired_state:
            if progress:
                progress.update(0.99, detail="{} reached {}".format(label, desired_state))
            return obj

        if time.time() - start > max_wait_seconds:
            raise TimeoutError("Timed out waiting for {} to reach {}. Last state={}".format(label, desired_state, state))

        if progress:
            progress.update(min(0.95, (time.time() - start) / float(max_wait_seconds)),
                            detail="{} state={} (waiting...)".format(label, state))
        time.sleep(interval)


# =============================================================================
# OCI discovery helpers
# =============================================================================

def get_instance_vnics(compute, network, compartment_id: str, instance_id: str):
    vnic_atts = list_call_get_all_results(
        compute.list_vnic_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data
    vnics = []
    for att in vnic_atts:
        vnic = network.get_vnic(att.vnic_id).data
        vnics.append((att, vnic))
    vnics.sort(key=lambda t: (not bool(getattr(t[0], "is_primary", False))))
    return vnics


def get_instance_boot_volume_id(compute, compartment_id: str, instance_id: str, availability_domain: str) -> str:
    bvas = list_call_get_all_results(
        compute.list_boot_volume_attachments,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        instance_id=instance_id
    ).data
    if not bvas:
        raise RuntimeError("No boot volume attachment found for instance {}.".format(instance_id))
    return bvas[0].boot_volume_id


def get_instance_block_volume_attachments(compute, compartment_id: str, instance_id: str):
    return list_call_get_all_results(
        compute.list_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data


def get_ip_to_subnet_map_for_instance(network, instance_vnics) -> Dict[str, str]:
    private_ip_map = get_private_ip_map_for_instance(network, instance_vnics)
    return {ip: meta["subnet_id"] for ip, meta in private_ip_map.items()}


def get_private_ip_map_for_instance(network, instance_vnics) -> Dict[str, Dict[str, Any]]:
    private_ip_map: Dict[str, Dict[str, Any]] = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        for pip in privs:
            private_ip_map[pip.ip_address] = {
                "subnet_id": vnic.subnet_id,
                "vnic_id": vnic.id,
                "is_primary": bool(getattr(pip, "is_primary", False)),
                "display_name": getattr(pip, "display_name", None),
            }
    return private_ip_map


def get_subnet_to_primary_ip_for_instance(network, instance_vnics) -> Dict[str, str]:
    subnet_to_ip: Dict[str, str] = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        primary = None
        for p in privs:
            if getattr(p, "is_primary", False):
                primary = p
                break
        if primary:
            subnet_to_ip[vnic.subnet_id] = primary.ip_address
    return subnet_to_ip


def pick_destination_ad(identity, tenancy_id: str, source_ad: str, requested_dest_ad: str) -> str:
    ads = identity.list_availability_domains(tenancy_id).data
    ad_names = [a.name for a in ads]
    if requested_dest_ad:
        if requested_dest_ad not in ad_names:
            raise ValueError("Requested --dest-ad '{}' not in region AD list: {}".format(requested_dest_ad, ad_names))
        if requested_dest_ad == source_ad:
            raise ValueError("Requested --dest-ad equals source AD. Choose a different AD.")
        return requested_dest_ad

    for name in ad_names:
        if name != source_ad:
            return name

    raise RuntimeError("Only one AD exists in this region; cannot migrate AD-to-AD.")


def validate_destination_networking(network, instance_vnics, dest_ad: str) -> List[Dict[str, Any]]:
    """
    Regional subnets can host VNICs in any AD. AD-specific subnets cannot.
    Fail before stopping the source if any existing VNIC subnet is pinned to a
    different availability domain.
    """
    subnet_cache: Dict[str, Any] = {}
    issues: List[str] = []
    summaries: List[Dict[str, Any]] = []

    for _, vnic in instance_vnics:
        if vnic.subnet_id not in subnet_cache:
            subnet_cache[vnic.subnet_id] = network.get_subnet(vnic.subnet_id).data
        subnet = subnet_cache[vnic.subnet_id]
        subnet_ad = getattr(subnet, "availability_domain", None)
        summaries.append({
            "subnet_id": vnic.subnet_id,
            "subnet_name": getattr(subnet, "display_name", None),
            "subnet_availability_domain": subnet_ad,
            "is_regional": subnet_ad is None,
        })
        if subnet_ad and subnet_ad != dest_ad:
            issues.append(
                "{} ({}) is AD-specific to {}, cannot attach in {}".format(
                    getattr(subnet, "display_name", vnic.subnet_id),
                    vnic.subnet_id,
                    subnet_ad,
                    dest_ad,
                )
            )

    if issues:
        raise RuntimeError(
            "Destination networking preflight failed:\n  - {}\n"
            "Use regional subnets, provide destination subnet mapping in a future enhancement, "
            "or choose a destination AD compatible with these subnets.".format("\n  - ".join(issues))
        )
    return summaries


def validate_lb_matches_are_primary_ips(lb_matches: List[Dict[str, Any]]) -> None:
    secondary = [m for m in lb_matches if not m.get("old_ip_is_primary", True)]
    if not secondary:
        return
    details = [
        "{} / {} backend {}:{}".format(
            m.get("lb_name"),
            m.get("backend_set_name"),
            m.get("old_ip"),
            m.get("port"),
        )
        for m in secondary
    ]
    raise RuntimeError(
        "--restore-lb found backend(s) on secondary private IPs, which Bulma cannot safely remap yet:\n"
        "  - {}\n"
        "Rerun without --restore-lb and recreate those backend entries manually after migration, "
        "or extend Bulma to recreate secondary private IPs first.".format("\n  - ".join(details))
    )


# =============================================================================
# Classic Load Balancer discovery & restore
# =============================================================================

def discover_classic_lb_membership(lb_client, lb_compartment_id: str, instance_private_ips: Dict[str, Dict[str, Any]],
                                   progress: Optional["StepProgress"] = None) -> List[Dict[str, Any]]:
    instance_ips = set(instance_private_ips.keys())
    matches: List[Dict[str, Any]] = []

    lbs = list_call_get_all_results(lb_client.list_load_balancers, compartment_id=lb_compartment_id).data
    total_lbs = max(1, len(lbs))

    for i, lb in enumerate(lbs, start=1):
        if progress:
            progress.update((i - 1) / float(total_lbs),
                            detail="Scanning LB {}/{}: {}".format(i, len(lbs), getattr(lb, "display_name", lb.id)))
        try:
            backend_sets = list_call_get_all_results(lb_client.list_backend_sets, load_balancer_id=lb.id).data
        except Exception as e:
            raise RuntimeError(
                "Failed to list backend sets for load balancer {} ({}): {}".format(
                    getattr(lb, "display_name", lb.id), lb.id, e
                )
            )

        for bs in backend_sets:
            bs_name = getattr(bs, "name", None)
            if not bs_name:
                continue
            try:
                backends = list_call_get_all_results(
                    lb_client.list_backends,
                    load_balancer_id=lb.id,
                    backend_set_name=bs_name
                ).data
            except Exception as e:
                raise RuntimeError(
                    "Failed to list backends for load balancer {} backend set {}: {}".format(
                        getattr(lb, "display_name", lb.id), bs_name, e
                    )
                )

            for be in backends:
                ip = getattr(be, "ip_address", None)
                port = getattr(be, "port", None)
                if ip in instance_ips and port is not None:
                    ip_meta = instance_private_ips.get(ip, {})
                    matches.append({
                        "lb_id": lb.id,
                        "lb_name": getattr(lb, "display_name", lb.id),
                        "backend_set_name": bs_name,
                        "old_ip": ip,
                        "port": int(port),
                        "weight": getattr(be, "weight", None),
                        "backup": bool(getattr(be, "backup", False)),
                        "max_connections": getattr(be, "max_connections", None),
                        "subnet_id": ip_meta.get("subnet_id"),
                        "old_ip_is_primary": bool(ip_meta.get("is_primary", False)),
                    })

    if progress:
        progress.update(0.99, detail="LB scan done. Matches found: {}".format(len(matches)))
    return matches


def ensure_backend_in_classic_lb(lb_ops, match: Dict[str, Any], new_ip: str, disable_old: bool,
                                  progress: Optional["StepProgress"] = None, idx: int = 0, total: int = 1) -> None:
    lb_id = match["lb_id"]
    bs = match["backend_set_name"]
    port = match["port"]

    if progress:
        progress.update((idx / float(max(1, total))) * 0.7,
                        detail="Adding backend {}:{} to {}/{} ({}/{})".format(
                            new_ip, port, match["lb_name"], bs, idx + 1, total
                        ))

    create_details = oci.load_balancer.models.CreateBackendDetails(
        ip_address=new_ip,
        port=port,
        weight=match["weight"] if match["weight"] is not None else 1,
        backup=match["backup"],
        drain=False,
        offline=False
    )
    if match["max_connections"] is not None:
        create_details.max_connections = match["max_connections"]

    try:
        lb_ops.create_backend_and_wait_for_state(
            load_balancer_id=lb_id,
            backend_set_name=bs,
            create_backend_details=create_details,
            wait_for_states=["SUCCEEDED"]
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (400, 409):
            backend_name = "{}:{}".format(new_ip, port)
            upd = oci.load_balancer.models.UpdateBackendDetails(
                weight=create_details.weight,
                backup=create_details.backup,
                drain=False,
                offline=False
            )
            if match["max_connections"] is not None:
                upd.max_connections = match["max_connections"]

            lb_ops.update_backend_and_wait_for_state(
                load_balancer_id=lb_id,
                backend_set_name=bs,
                backend_name=backend_name,
                update_backend_details=upd,
                wait_for_states=["SUCCEEDED"]
            )
        else:
            raise

    if disable_old:
        if progress:
            progress.update((idx / float(max(1, total))) * 0.7 + 0.2,
                            detail="Disabling OLD backend {}:{} (DRAIN+OFFLINE)".format(match["old_ip"], port))
        old_backend_name = "{}:{}".format(match["old_ip"], port)
        upd_old = oci.load_balancer.models.UpdateBackendDetails(
            weight=match["weight"] if match["weight"] is not None else 1,
            backup=match["backup"],
            drain=True,
            offline=True
        )
        if match["max_connections"] is not None:
            upd_old.max_connections = match["max_connections"]

        lb_ops.update_backend_and_wait_for_state(
            load_balancer_id=lb_id,
            backend_set_name=bs,
            backend_name=old_backend_name,
            update_backend_details=upd_old,
            wait_for_states=["SUCCEEDED"]
        )

    if progress:
        progress.update(((idx + 1) / float(max(1, total))) * 0.95,
                        detail="LB backend updated: {}/{}".format(match["lb_name"], bs))


# =============================================================================
# Migration steps
# =============================================================================

def stop_instance_if_needed(compute, instance, max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> bool:
    if instance.lifecycle_state == "STOPPED":
        if progress:
            progress.update(1.0, detail="Instance already STOPPED")
        return False
    if progress:
        progress.update(0.05, detail="Sending STOP action")
    compute.instance_action(instance.id, "STOP")
    instance_id = instance.id  # capture now to avoid late-binding closure bug
    wait_for_state(lambda: compute.get_instance(instance_id).data, "STOPPED", max_wait, poll, "instance", progress)
    if progress:
        progress.update(1.0, detail="Instance STOPPED")
    return True


def start_instance_if_needed(compute, instance_id: str, max_wait: int, poll: int,
                             progress: Optional["StepProgress"] = None) -> None:
    inst = compute.get_instance(instance_id).data
    if inst.lifecycle_state == "RUNNING":
        if progress:
            progress.update(1.0, detail="Source instance already RUNNING")
        return
    if progress:
        progress.update(0.05, detail="Attempting source instance START after failure")
    compute.instance_action(instance_id, "START")
    wait_for_state(lambda: compute.get_instance(instance_id).data, "RUNNING", max_wait, poll, "source restart", progress)


def backup_boot_volume(block, boot_volume_id: str, name_prefix: str, max_wait: int, poll: int,
                       freeform_tags: Optional[Dict[str, str]] = None,
                       progress: Optional["StepProgress"] = None) -> str:
    display_name = "{}-boot-bkp-{}".format(name_prefix, datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    if progress:
        progress.update(0.05, detail="Creating boot backup: {}".format(display_name))
    details = oci.core.models.CreateBootVolumeBackupDetails(
        boot_volume_id=boot_volume_id,
        display_name=display_name,
        type="FULL",
        freeform_tags=freeform_tags
    )
    bkp = block.create_boot_volume_backup(details).data
    bkp_id = bkp.id  # capture now to avoid late-binding closure bug
    wait_for_state(lambda: block.get_boot_volume_backup(bkp_id).data, "AVAILABLE", max_wait, poll, "boot backup", progress)
    if progress:
        progress.update(1.0, detail="Boot backup AVAILABLE")
    return bkp_id


def backup_block_volumes(block, volume_attachments, name_prefix: str, max_wait: int, poll: int,
                         freeform_tags: Optional[Dict[str, str]] = None,
                         progress: Optional["StepProgress"] = None) -> Dict[str, str]:
    backup_map: Dict[str, str] = {}
    total = len(volume_attachments)
    if total == 0:
        if progress:
            progress.update(1.0, detail="No attached block volumes to backup")
        return backup_map

    for i, att in enumerate(volume_attachments, start=1):
        vol_id = att.volume_id
        display_name = "{}-blk-bkp-{}-{}".format(name_prefix, vol_id[-6:], datetime.utcnow().strftime("%Y%m%d%H%M%S"))
        if progress:
            progress.update((i - 1) / float(total), detail="Creating block backup {}/{}: {}".format(i, total, display_name))
        details = oci.core.models.CreateVolumeBackupDetails(
            volume_id=vol_id,
            display_name=display_name,
            type="FULL",
            freeform_tags=freeform_tags
        )
        bkp = block.create_volume_backup(details).data
        bkp_id = bkp.id  # capture now to avoid late-binding closure bug
        wait_for_state(
            lambda bid=bkp_id: block.get_volume_backup(bid).data,
            "AVAILABLE", max_wait, poll, "block backup {}/{}".format(i, total), progress
        )
        backup_map[vol_id] = bkp_id
        if progress:
            progress.update(i / float(total), detail="Block backup {}/{} AVAILABLE".format(i, total))

    return backup_map


def restore_boot_volume(block, compartment_id: str, dest_ad: str, boot_backup_id: str, name_prefix: str,
                        max_wait: int, poll: int, freeform_tags: Optional[Dict[str, str]] = None,
                        progress: Optional["StepProgress"] = None) -> str:
    display_name = "{}-boot-restored".format(name_prefix)
    if progress:
        progress.update(0.05, detail="Restoring boot volume into {}: {}".format(dest_ad, display_name))

    source = oci.core.models.BootVolumeSourceFromBootVolumeBackupDetails(id=boot_backup_id)
    details = oci.core.models.CreateBootVolumeDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=display_name,
        source_details=source,
        freeform_tags=freeform_tags
    )
    bv = block.create_boot_volume(details).data
    bv_id = bv.id  # capture now to avoid late-binding closure bug
    wait_for_state(lambda: block.get_boot_volume(bv_id).data, "AVAILABLE", max_wait, poll, "restored boot volume", progress)
    if progress:
        progress.update(1.0, detail="Restored boot volume AVAILABLE")
    return bv_id


def restore_block_volumes(block, compartment_id: str, dest_ad: str, volume_backup_map: Dict[str, str], name_prefix: str,
                          max_wait: int, poll: int, freeform_tags: Optional[Dict[str, str]] = None,
                          progress: Optional["StepProgress"] = None) -> Dict[str, str]:
    restored: Dict[str, str] = {}
    total = len(volume_backup_map)
    if total == 0:
        if progress:
            progress.update(1.0, detail="No block volumes to restore")
        return restored

    items = list(volume_backup_map.items())
    for i, (orig_vol_id, backup_id) in enumerate(items, start=1):
        display_name = "{}-blk-restored-{}".format(name_prefix, orig_vol_id[-6:])
        if progress:
            progress.update((i - 1) / float(total),
                            detail="Restoring block volume {}/{} into {}: {}".format(i, total, dest_ad, display_name))

        source = oci.core.models.VolumeSourceFromVolumeBackupDetails(id=backup_id)
        details = oci.core.models.CreateVolumeDetails(
            availability_domain=dest_ad,
            compartment_id=compartment_id,
            display_name=display_name,
            source_details=source,
            freeform_tags=freeform_tags
        )
        vol = block.create_volume(details).data
        vol_id = vol.id  # capture now to avoid late-binding closure bug
        wait_for_state(
            lambda vid=vol_id: block.get_volume(vid).data,
            "AVAILABLE", max_wait, poll, "restored block volume {}/{}".format(i, total), progress
        )
        restored[orig_vol_id] = vol_id
        if progress:
            progress.update(i / float(total), detail="Restored block volume {}/{} AVAILABLE".format(i, total))

    return restored


def create_new_instance(compute, instance, compartment_id: str, dest_ad: str, boot_volume_id: str, primary_vnic,
                        new_shape: str, ssh_key: str, run_id: str, copy_tags: bool, max_wait: int, poll: int,
                        progress: Optional["StepProgress"] = None) -> Tuple[str, str]:
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = "{}-Bulma-{}".format(instance.display_name, suffix)
    shape = new_shape if new_shape else instance.shape

    # Carry over ShapeConfig for flexible shapes (e.g. VM.Standard.E4.Flex).
    # Flex shapes require explicit ocpus + memory_in_gbs — omitting this causes a 400.
    shape_config = None
    orig_sc = getattr(instance, "shape_config", None)
    if orig_sc is not None and shape == instance.shape:
        ocpus = getattr(orig_sc, "ocpus", None)
        memory_in_gbs = getattr(orig_sc, "memory_in_gbs", None)
        if ocpus is not None and memory_in_gbs is not None:
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=float(ocpus),
                memory_in_gbs=float(memory_in_gbs)
            )
            # Preserve baseline OCPU utilisation if set (e.g. BASELINE_1_8, BASELINE_1_2)
            baseline = getattr(orig_sc, "baseline_ocpu_utilization", None)
            if baseline:
                shape_config.baseline_ocpu_utilization = baseline

    if progress:
        sc_info = " ocpus={} mem={}GB".format(
            getattr(shape_config, "ocpus", "?"),
            getattr(shape_config, "memory_in_gbs", "?")
        ) if shape_config else ""
        progress.update(0.05, detail="Launching NEW instance: {} (shape={}{})".format(new_name, shape, sc_info))

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id=boot_volume_id)

    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

    run_tags = bulma_freeform_tags(run_id, instance.id)
    freeform_tags = merge_freeform_tags(
        getattr(instance, "freeform_tags", {}) if copy_tags else {},
        run_tags
    )
    defined_tags = getattr(instance, "defined_tags", None) if copy_tags else None

    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=primary_vnic.subnet_id,
        display_name=primary_vnic.display_name or "{}-vnic0".format(new_name),
        assign_public_ip=bool(getattr(primary_vnic, "public_ip", None)),
        nsg_ids=list(primary_vnic.nsg_ids or [])
    )

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=new_name,
        shape=shape,
        shape_config=shape_config,
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        metadata=metadata,
        freeform_tags=freeform_tags,
        defined_tags=defined_tags
    )

    new_inst = compute.launch_instance(details).data
    new_inst_id = new_inst.id  # capture now to avoid late-binding closure bug
    wait_for_state(lambda: compute.get_instance(new_inst_id).data, "RUNNING", max_wait, poll, "new instance", progress)
    if progress:
        progress.update(1.0, detail="New instance RUNNING")
    return new_inst_id, new_name


def attach_secondary_vnics(compute, instance_vnics, new_instance_id: str, max_wait: int, poll: int,
                           progress: Optional["StepProgress"] = None) -> List[str]:
    if len(instance_vnics) <= 1:
        if progress:
            progress.update(1.0, detail="No secondary VNICs to recreate")
        return []

    created: List[str] = []
    total = len(instance_vnics) - 1
    for idx, (_, vnic) in enumerate(instance_vnics[1:], start=1):
        if progress:
            progress.update((idx - 1) / float(total),
                            detail="Attaching secondary VNIC {}/{} (subnet+NSGs preserved)".format(idx, total))

        create_vnic = oci.core.models.CreateVnicDetails(
            subnet_id=vnic.subnet_id,
            display_name=vnic.display_name or "vnic{}".format(idx),
            assign_public_ip=bool(getattr(vnic, "public_ip", None)),
            nsg_ids=list(vnic.nsg_ids or [])
        )
        details = oci.core.models.CreateVnicAttachmentDetails(
            instance_id=new_instance_id, create_vnic_details=create_vnic
        )
        va = compute.attach_vnic(details).data
        va_id = va.id  # capture now to avoid late-binding closure bug
        wait_for_state(
            lambda vid=va_id: compute.get_vnic_attachment(vid).data,
            "ATTACHED", max_wait, poll, "vnic_attachment {}/{}".format(idx, total), progress
        )
        created.append(va_id)
        if progress:
            progress.update(idx / float(total), detail="Secondary VNIC {}/{} ATTACHED".format(idx, total))

    return created


def attach_restored_volumes(compute, new_instance_id: str, original_attachments, restored_map: Dict[str, str],
                            max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> List[str]:
    attached: List[str] = []
    candidates = [a for a in original_attachments if restored_map.get(a.volume_id)]
    total = len(candidates)
    if total == 0:
        if progress:
            progress.update(1.0, detail="No restored block volumes to attach")
        return attached

    for i, att in enumerate(candidates, start=1):
        new_vol_id = restored_map[att.volume_id]
        atype = (att.attachment_type or "").upper()
        is_read_only = bool(getattr(att, "is_read_only", False))

        if progress:
            progress.update((i - 1) / float(total),
                            detail="Attaching volume {}/{} (type={}, ro={})".format(i, total, atype, is_read_only))

        if atype == "ISCSI":
            details = oci.core.models.AttachIScsiVolumeDetails(
                instance_id=new_instance_id,
                volume_id=new_vol_id,
                is_read_only=is_read_only,
                display_name=getattr(att, "display_name", None)
            )
        else:
            details = oci.core.models.AttachParavirtualizedVolumeDetails(
                instance_id=new_instance_id,
                volume_id=new_vol_id,
                is_read_only=is_read_only,
                display_name=getattr(att, "display_name", None)
            )
            device = getattr(att, "device", None)
            if device:
                details.device = device

        va = compute.attach_volume(details).data
        va_id = va.id  # capture now to avoid late-binding closure bug
        wait_for_state(
            lambda vid=va_id: compute.get_volume_attachment(vid).data,
            "ATTACHED", max_wait, poll, "volume_attachment {}/{}".format(i, total), progress
        )
        attached.append(va_id)
        if progress:
            progress.update(i / float(total), detail="Volume {}/{} ATTACHED".format(i, total))

    return attached


# =============================================================================
# Main
# =============================================================================

def main():
    args = parse_args()
    ssh_key = read_ssh_key(args)
    run_id = "{}-{}".format(datetime.utcnow().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    manifest_path = args.manifest.strip() or default_manifest_path()
    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": _ts(),
        "dry_run": bool(args.dry_run),
        "restore_lb": bool(args.restore_lb),
        "restart_source_on_failure": bool(args.restart_source_on_failure),
        "instances": [],
    }
    write_manifest(manifest_path, manifest)
    print("{} | [Manifest] {}".format(_ts(), os.path.abspath(manifest_path)), flush=True)

    # Validate + infer region
    for ocid in args.instance_ids:
        if not ocid.startswith("ocid1.instance"):
            raise ValueError("Not an instance OCID: {}".format(ocid))

    regions = sorted(set(infer_region_from_instance_ocid(x) for x in args.instance_ids))
    if len(regions) != 1:
        raise ValueError("All instance OCIDs must be in the same region. Found regions: {}".format(regions))
    region_hint = regions[0]

    # Auth is validated here — before any migration work starts
    config, signer = get_signer_and_config(region_hint, profile=args.profile)
    identity, compute, network, block, lb, lb_ops = get_clients(config, signer)
    tenancy_id = get_tenancy_id(config, signer)

    multi = MultiProgress(len(args.instance_ids))

    for n, instance_id in enumerate(args.instance_ids, start=1):
        instance_entry: Dict[str, Any] = {
            "source_instance_id": instance_id,
            "status": "started",
            "created_resources": {},
        }
        manifest["instances"].append(instance_entry)
        write_manifest(manifest_path, manifest)

        steps = [
            ("Discover configuration", 10),
            ("Scan Load Balancers", 8 if args.restore_lb else 0),
            ("Stop source instance", 12),
            ("Backup boot volume", 12),
            ("Backup block volumes", 18),
            ("Restore boot volume", 12),
            ("Restore block volumes", 18),
            ("Launch new instance", 12),
            ("Attach secondary VNICs", 6),
            ("Attach block volumes", 10),
            ("Restore LB backends", 10 if args.restore_lb else 0),
        ]

        prog = StepProgress(
            label="Bulma | {}".format(instance_id[-10:]),
            steps=steps,
            overall_prefix=multi.overall_prefix(n)
        )

        source_stopped_by_bulma = False
        try:
            prog.start_step("Discover configuration")
            inst = compute.get_instance(instance_id).data
            prog.label = "Bulma | {}".format(inst.display_name)

            compartment_id = inst.compartment_id
            source_ad = inst.availability_domain
            dest_ad = pick_destination_ad(identity, tenancy_id, source_ad, args.dest_ad)

            instance_vnics = get_instance_vnics(compute, network, compartment_id, instance_id)
            if not instance_vnics:
                raise RuntimeError("No VNIC attachments found for instance {}.".format(instance_id))
            _, primary_vnic = instance_vnics[0]

            network_summary = validate_destination_networking(network, instance_vnics, dest_ad)
            private_ip_map = get_private_ip_map_for_instance(network, instance_vnics)
            secondary_private_ips = sorted(
                ip for ip, meta in private_ip_map.items() if not meta.get("is_primary", False)
            )

            boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, instance_id, source_ad)
            block_attachments = get_instance_block_volume_attachments(compute, compartment_id, instance_id)

            instance_entry.update({
                "display_name": inst.display_name,
                "compartment_id": compartment_id,
                "region": config.get("region"),
                "source_ad": source_ad,
                "destination_ad": dest_ad,
                "boot_volume_id": boot_volume_id,
                "block_volume_count": len(block_attachments),
                "vnic_count": len(instance_vnics),
                "secondary_private_ips": secondary_private_ips,
                "network_preflight": network_summary,
            })
            write_manifest(manifest_path, manifest)

            prog.update(0.7, detail="region={} | sourceAD={} -> destAD={} | vnics={} | blockVols={} | secondaryIPs={}".format(
                config.get("region"), source_ad, dest_ad, len(instance_vnics), len(block_attachments), len(secondary_private_ips)
            ))
            prog.complete_step("Configuration collected")

            # LB scan
            lb_matches: List[Dict[str, Any]] = []
            lb_compartment_id = args.lb_compartment_id.strip() or compartment_id
            if args.restore_lb:
                prog.start_step("Scan Load Balancers")
                prog.update(0.05, detail="scanning classic LBs in compartment {}".format(lb_compartment_id))
                lb_matches = discover_classic_lb_membership(lb, lb_compartment_id, private_ip_map, progress=prog)
                validate_lb_matches_are_primary_ips(lb_matches)
                instance_entry["lb_matches"] = lb_matches
                write_manifest(manifest_path, manifest)
                prog.complete_step("LB matches={}".format(len(lb_matches)))

            if args.dry_run:
                instance_entry["status"] = "dry_run_complete"
                write_manifest(manifest_path, manifest)
                print("{} | [DryRun] {}: preflight passed; no changes made.".format(_ts(), inst.display_name), flush=True)
                continue

            run_tags = bulma_freeform_tags(run_id, instance_id)

            # Stop
            prog.start_step("Stop source instance")
            source_stopped_by_bulma = stop_instance_if_needed(compute, inst, args.wait, args.poll, progress=prog)
            instance_entry["source_stopped_by_bulma"] = source_stopped_by_bulma
            write_manifest(manifest_path, manifest)
            prog.complete_step("Source instance STOPPED")

            name_prefix = inst.display_name.replace(" ", "_")[:40]

            # Backup boot
            prog.start_step("Backup boot volume")
            boot_bkp_id = backup_boot_volume(
                block, boot_volume_id, name_prefix, args.wait, args.poll, freeform_tags=run_tags, progress=prog
            )
            instance_entry["created_resources"]["boot_volume_backup_id"] = boot_bkp_id
            write_manifest(manifest_path, manifest)
            prog.complete_step("Boot backup complete")

            # Backup block
            prog.start_step("Backup block volumes")
            blk_bkp_map = backup_block_volumes(
                block, block_attachments, name_prefix, args.wait, args.poll, freeform_tags=run_tags, progress=prog
            )
            instance_entry["created_resources"]["block_volume_backup_ids_by_source_volume_id"] = blk_bkp_map
            write_manifest(manifest_path, manifest)
            prog.complete_step("Block backups complete (count={})".format(len(blk_bkp_map)))

            # Restore boot
            prog.start_step("Restore boot volume")
            new_boot_vol_id = restore_boot_volume(
                block, compartment_id, dest_ad, boot_bkp_id, name_prefix, args.wait, args.poll,
                freeform_tags=run_tags, progress=prog
            )
            instance_entry["created_resources"]["restored_boot_volume_id"] = new_boot_vol_id
            write_manifest(manifest_path, manifest)
            prog.complete_step("Boot restore complete")

            # Restore block
            prog.start_step("Restore block volumes")
            restored_blk_map = restore_block_volumes(
                block, compartment_id, dest_ad, blk_bkp_map, name_prefix, args.wait, args.poll,
                freeform_tags=run_tags, progress=prog
            )
            instance_entry["created_resources"]["restored_block_volume_ids_by_source_volume_id"] = restored_blk_map
            write_manifest(manifest_path, manifest)
            prog.complete_step("Block restore complete (count={})".format(len(restored_blk_map)))

            # Launch new instance
            prog.start_step("Launch new instance")
            new_instance_id, new_instance_name = create_new_instance(
                compute, inst, compartment_id, dest_ad, new_boot_vol_id, primary_vnic,
                args.shape, ssh_key, run_id, args.copy_tags, args.wait, args.poll, progress=prog
            )
            instance_entry["created_resources"]["new_instance_id"] = new_instance_id
            instance_entry["created_resources"]["new_instance_name"] = new_instance_name
            write_manifest(manifest_path, manifest)
            prog.complete_step("New instance running")

            # Secondary VNICs
            prog.start_step("Attach secondary VNICs")
            secondary_vnic_attachment_ids = attach_secondary_vnics(
                compute, instance_vnics, new_instance_id, args.wait, args.poll, progress=prog
            )
            instance_entry["created_resources"]["secondary_vnic_attachment_ids"] = secondary_vnic_attachment_ids
            write_manifest(manifest_path, manifest)
            prog.complete_step("Secondary VNICs attached")

            # Attach volumes
            prog.start_step("Attach block volumes")
            volume_attachment_ids = attach_restored_volumes(
                compute, new_instance_id, block_attachments, restored_blk_map, args.wait, args.poll, progress=prog
            )
            instance_entry["created_resources"]["volume_attachment_ids"] = volume_attachment_ids
            write_manifest(manifest_path, manifest)
            prog.complete_step("Volumes attached")

            # LB restore
            if args.restore_lb:
                prog.start_step("Restore LB backends")
                if lb_matches:
                    new_instance_vnics = get_instance_vnics(compute, network, compartment_id, new_instance_id)
                    new_subnet_to_ip = get_subnet_to_primary_ip_for_instance(network, new_instance_vnics)
                    new_primary_private_ip = new_subnet_to_ip.get(primary_vnic.subnet_id)

                    total = len(lb_matches)
                    for i, m in enumerate(lb_matches):
                        desired_ip = new_subnet_to_ip.get(m.get("subnet_id")) or new_primary_private_ip
                        if not desired_ip:
                            raise RuntimeError(
                                "Could not determine new private IP for LB backend {}:{}.".format(
                                    m.get("old_ip"), m.get("port")
                                )
                            )
                        ensure_backend_in_classic_lb(
                            lb_ops, m, desired_ip, args.lb_disable_old, progress=prog, idx=i, total=total
                        )
                    instance_entry["lb_restored"] = True
                    write_manifest(manifest_path, manifest)
                    prog.complete_step("LB restored")
                else:
                    instance_entry["lb_restored"] = False
                    write_manifest(manifest_path, manifest)
                    prog.update(1.0, detail="No LB membership detected; nothing to restore")
                    prog.complete_step("Skipped")

            instance_entry["status"] = "complete"
            write_manifest(manifest_path, manifest)

        except Exception as exc:
            instance_entry["status"] = "failed"
            instance_entry["error"] = str(exc)
            write_manifest(manifest_path, manifest)
            if args.restart_source_on_failure and source_stopped_by_bulma:
                try:
                    start_instance_if_needed(compute, instance_id, args.wait, args.poll, progress=prog)
                    instance_entry["source_restart_after_failure"] = "succeeded"
                except Exception as restart_exc:
                    instance_entry["source_restart_after_failure"] = "failed: {}".format(restart_exc)
                write_manifest(manifest_path, manifest)
            raise

    manifest["status"] = "dry_run_complete" if args.dry_run else "complete"
    write_manifest(manifest_path, manifest)
    print("{} | [Manifest] Final manifest written: {}".format(_ts(), os.path.abspath(manifest_path)), flush=True)


if __name__ == "__main__":
    try:
        main()
        print("\n{} | All done. Rocket!".format(_ts()))
    except KeyboardInterrupt:
        print("\n{} | Canceled by user.".format(_ts()))
        sys.exit(130)
    except Exception as e:
        if "--debug" in sys.argv:
            traceback.print_exc()
        print("\n{} | ERROR: {}".format(_ts(), e))
        sys.exit(1)
