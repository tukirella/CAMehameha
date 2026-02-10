#!/usr/bin/env python3
"""
bulma.py — OCI AD-to-AD Compute migration via Backup & Restore (keeps original instance)

✔ Safe migration model:
- Operator selects 1+ instances (interactive)
- Source instance is STOPPED (for consistent backups)
- Boot volume + attached block volumes are backed up
- Volumes are restored into a destination Availability Domain (same region)
- A NEW instance is launched from the restored boot volume
- Network is recreated (same subnet + NSGs) for primary + secondary VNICs (NEW private IPs)
- Restored block volumes are re-attached
- OPTIONAL: classic Load Balancer membership restore (LBaaS): add NEW backend(s) into same backend set(s)
  - Optional: set OLD backend(s) to DRAIN + OFFLINE

✔ Maximum visibility:
- Each action reports: overall % + current step % + live details
- Works on older Cloud Shell Python (no "X | None" typing)

Run:
  chmod +x bulma.py
  python3 bulma.py
Optional:
  python3 bulma.py --restore-lb
  python3 bulma.py --restore-lb --lb-disable-old
  python3 bulma.py --restore-lb --lb-compartment-id ocid1.compartment...

Notes:
- Classic OCI Load Balancer backends are IP:port; we detect membership by matching any private IP on any VNIC of the source instance.
- Network Load Balancer (NLB) is not handled here yet.
"""

import argparse
import sys
import time
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

import oci
from oci.pagination import list_call_get_all_results


# =============================================================================
# Progress / Visibility (0–100%)
# =============================================================================

def _ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")


def _bar(pct: int, width: int = 28) -> str:
    pct = max(0, min(100, pct))
    filled = int(round(width * (pct / 100.0)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


class StepProgress:
    """
    Tracks one unit of work (e.g., one instance migration) with weighted steps.

    Prints:
      - overall 0–100%
      - step 0–100%
      - step label + details
    """

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
                self._print(overall_pct=self._completed_weight, step_pct=0, detail="START")
                return
        raise ValueError("Step '{}' not found in steps list.".format(name))

    def update(self, step_frac: float, detail: str = "") -> None:
        step_frac = max(0.0, min(1.0, step_frac))
        step_pct = int(round(step_frac * 100))
        overall = int(round(self._completed_weight + (step_frac * self._current_step_weight)))
        self._print(overall_pct=overall, step_pct=step_pct, detail=detail)

    def complete_step(self, detail: str = "DONE") -> None:
        self._print(overall_pct=self._completed_weight + self._current_step_weight, step_pct=100, detail=detail)
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
            line += " — " + detail
        print(line, flush=True)


class MultiProgress:
    def __init__(self, total_instances: int):
        self.total = max(1, int(total_instances))

    def overall_prefix(self, instance_index_1based: int) -> str:
        return "[{}/{}]".format(instance_index_1based, self.total)


# =============================================================================
# Auth / client setup
# =============================================================================

def get_signer_and_config():
    # Try Resource Principals first; otherwise ~/.oci/config
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        config = {"region": signer.region, "tenancy": getattr(signer, "tenancy_id", None)}
        print("{} | [Auth] Using Resource Principals signer (region={})".format(_ts(), config["region"]), flush=True)
        return config, signer
    except Exception:
        config = oci.config.from_file()
        signer = None
        print("{} | [Auth] Using config file (~/.oci/config) (region={})".format(_ts(), config.get("region")), flush=True)
        return config, signer


def get_tenancy_id(config: Dict[str, Any], signer) -> str:
    if config.get("tenancy"):
        return config["tenancy"]
    if signer is not None and getattr(signer, "tenancy_id", None):
        return signer.tenancy_id
    tid = input("Enter TENANCY OCID (required to list Availability Domains): ").strip()
    if not tid.startswith("ocid1.tenancy"):
        raise ValueError("Tenancy OCID does not look right.")
    return tid


def get_clients(config: Dict[str, Any], signer=None):
    identity = oci.identity.IdentityClient(config, signer=signer)
    compute = oci.core.ComputeClient(config, signer=signer)
    network = oci.core.VirtualNetworkClient(config, signer=signer)
    block = oci.core.BlockstorageClient(config, signer=signer)
    lb = oci.load_balancer.LoadBalancerClient(config, signer=signer)
    lb_ops = oci.load_balancer.LoadBalancerClientCompositeOperations(lb)
    return identity, compute, network, block, lb, lb_ops


# =============================================================================
# CLI / basic helpers
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="Bulma: AD-to-AD migration via backup/restore (keeps original instance).")
    p.add_argument("--restore-lb", action="store_true", help="Detect classic LB membership and re-add new backend(s).")
    p.add_argument("--lb-disable-old", action="store_true", help="After adding new backend(s), set old backend(s) drain+offline.")
    p.add_argument("--lb-compartment-id", default="", help="Compartment OCID where the Load Balancer(s) live (default: same as instance compartment).")
    return p.parse_args()


def prompt(msg: str, default: Optional[str] = None) -> str:
    if default is not None:
        val = input("{} [{}]: ".format(msg, default)).strip()
        return val if val else default
    return input(msg + ": ").strip()


def yn(msg: str, default: str = "y") -> bool:
    d = default.lower()
    suffix = " [Y/n]" if d == "y" else " [y/N]"
    while True:
        raw = input(msg + suffix + ": ").strip().lower()
        if not raw:
            return d == "y"
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y/n.")


def choose_from_list(title: str, items: List[Any], render_fn) -> Any:
    print("\n" + title)
    print("-" * len(title))
    for idx, it in enumerate(items, 1):
        print("{:>2}. {}".format(idx, render_fn(it)))
    while True:
        raw = input("Select number: ").strip()
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(items):
                return items[n - 1]
        print("Invalid selection, try again.")


def choose_multi_indices(items_len: int) -> List[int]:
    while True:
        raw = input("Select instance(s) by number (e.g. 1 or 1,3,7): ").strip()
        try:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            idxs = sorted(set(int(p) for p in parts))
            if not idxs:
                raise ValueError
            if any(i < 1 or i > items_len for i in idxs):
                raise ValueError
            return [i - 1 for i in idxs]
        except Exception:
            print("Invalid input, try again.")


def wait_for_state(
    getter_fn,
    desired_state: str,
    max_wait_seconds: int = 3600,
    interval: int = 10,
    label: str = "resource",
    progress: Optional["StepProgress"] = None
):
    """
    Polls getter_fn() until lifecycle_state == desired_state.
    If progress is supplied, we update within-step based on elapsed/max_wait (capped at 95% until done).
    """
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
            else:
                print("{} | - waiting for {}: {} -> {} ...".format(_ts(), label, state, desired_state), flush=True)

        if state == desired_state:
            if progress:
                progress.update(0.99, detail="{} reached {}".format(label, desired_state))
            return obj

        if time.time() - start > max_wait_seconds:
            raise TimeoutError("Timed out waiting for {} to reach {}. Last state: {}".format(label, desired_state, state))

        if progress:
            progress.update(min(0.95, (time.time() - start) / float(max_wait_seconds)),
                            detail="{} state={} (waiting...)".format(label, state))

        time.sleep(interval)


# =============================================================================
# OCI discovery
# =============================================================================

def list_instances_in_compartment(compute, compartment_id: str):
    resp = list_call_get_all_results(
        compute.list_instances,
        compartment_id=compartment_id,
        sort_by="DISPLAYNAME",
        sort_order="ASC"
    )
    return [i for i in resp.data if i.lifecycle_state != "TERMINATED"]


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


def get_instance_boot_volume_id(compute, compartment_id: str, instance_id: str) -> str:
    bvas = list_call_get_all_results(
        compute.list_boot_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data
    if not bvas:
        raise RuntimeError("No boot volume attachment found.")
    return bvas[0].boot_volume_id


def get_instance_block_volume_attachments(compute, compartment_id: str, instance_id: str):
    return list_call_get_all_results(
        compute.list_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data


def get_ip_to_subnet_map_for_instance(network, instance_vnics) -> Dict[str, str]:
    """
    dict[ip_address] = subnet_id for ALL private IPs on ALL VNICs
    """
    ip_to_subnet: Dict[str, str] = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        for pip in privs:
            ip_to_subnet[pip.ip_address] = vnic.subnet_id
    return ip_to_subnet


def get_subnet_to_primary_ip_for_instance(network, instance_vnics) -> Dict[str, str]:
    """
    dict[subnet_id] = primary_private_ip for each VNIC/subnet
    """
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


# =============================================================================
# Classic Load Balancer discovery & restore
# =============================================================================

def discover_classic_lb_membership(
    lb_client,
    lb_compartment_id: str,
    instance_ip_to_subnet: Dict[str, str],
    progress: Optional["StepProgress"] = None
) -> List[Dict[str, Any]]:
    """
    Finds classic LB backend entries that match any instance IP.
    Returns list of dicts:
      { lb_id, lb_name, backend_set_name, old_ip, port, weight, backup, drain, offline, max_connections, subnet_id }
    """
    instance_ips = set(instance_ip_to_subnet.keys())
    matches: List[Dict[str, Any]] = []

    lbs = list_call_get_all_results(lb_client.list_load_balancers, compartment_id=lb_compartment_id).data
    total_lbs = max(1, len(lbs))

    for i, lb in enumerate(lbs, start=1):
        if progress:
            progress.update((i - 1) / float(total_lbs),
                            detail="Scanning LB {}/{}: {}".format(i, len(lbs), getattr(lb, "display_name", lb.id)))
        try:
            backend_sets = list_call_get_all_results(lb_client.list_backend_sets, load_balancer_id=lb.id).data
        except Exception:
            continue

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
            except Exception:
                continue

            for be in backends:
                ip = getattr(be, "ip_address", None)
                port = getattr(be, "port", None)
                if ip in instance_ips and port is not None:
                    matches.append({
                        "lb_id": lb.id,
                        "lb_name": getattr(lb, "display_name", lb.id),
                        "backend_set_name": bs_name,
                        "old_ip": ip,
                        "port": int(port),
                        "weight": getattr(be, "weight", None),
                        "backup": bool(getattr(be, "backup", False)),
                        "drain": bool(getattr(be, "drain", False)),
                        "offline": bool(getattr(be, "offline", False)),
                        "max_connections": getattr(be, "max_connections", None),
                        "subnet_id": instance_ip_to_subnet.get(ip)
                    })

    if progress:
        progress.update(0.99, detail="LB scan done. Matches found: {}".format(len(matches)))

    return matches


def ensure_backend_in_classic_lb(
    lb_ops,
    match: Dict[str, Any],
    new_ip: str,
    disable_old: bool,
    progress: Optional["StepProgress"] = None,
    idx: int = 0,
    total: int = 1
) -> None:
    """
    Adds/updates backend in classic LB backend set; optionally disables old backend.
    Waits for work request SUCCEEDED using composite operations.
    """
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
        # often exists; update instead
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

def stop_instance_if_needed(compute, instance, progress: Optional["StepProgress"] = None) -> None:
    if instance.lifecycle_state == "STOPPED":
        if progress:
            progress.update(1.0, detail="Instance already STOPPED")
        return

    if progress:
        progress.update(0.05, detail="Sending STOP action")
    compute.instance_action(instance.id, "STOP")
    wait_for_state(lambda: compute.get_instance(instance.id).data, "STOPPED",
                   label="instance", progress=progress)
    if progress:
        progress.update(1.0, detail="Instance STOPPED")


def backup_boot_volume(block, boot_volume_id: str, name_prefix: str, progress: Optional["StepProgress"] = None) -> str:
    display_name = "{}-boot-bkp-{}".format(name_prefix, datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    if progress:
        progress.update(0.05, detail="Creating boot backup: {}".format(display_name))

    details = oci.core.models.CreateBootVolumeBackupDetails(
        boot_volume_id=boot_volume_id,
        display_name=display_name,
        type="FULL"
    )
    bkp = block.create_boot_volume_backup(details).data
    wait_for_state(lambda: block.get_boot_volume_backup(bkp.id).data, "AVAILABLE",
                   label="boot backup", progress=progress)
    if progress:
        progress.update(1.0, detail="Boot backup AVAILABLE")
    return bkp.id


def backup_block_volumes(block, volume_attachments, name_prefix: str, progress: Optional["StepProgress"] = None) -> Dict[str, str]:
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
            progress.update((i - 1) / float(total),
                            detail="Creating block backup {}/{}: {}".format(i, total, display_name))

        details = oci.core.models.CreateVolumeBackupDetails(
            volume_id=vol_id,
            display_name=display_name,
            type="FULL"
        )
        bkp = block.create_volume_backup(details).data
        wait_for_state(lambda: block.get_volume_backup(bkp.id).data, "AVAILABLE",
                       label="block backup {}/{}".format(i, total), progress=progress)
        backup_map[vol_id] = bkp.id

        if progress:
            progress.update(i / float(total), detail="Block backup {}/{} AVAILABLE".format(i, total))

    return backup_map


def restore_boot_volume(block, compartment_id: str, dest_ad: str, boot_backup_id: str, name_prefix: str,
                        progress: Optional["StepProgress"] = None) -> str:
    display_name = "{}-boot-restored".format(name_prefix)
    if progress:
        progress.update(0.05, detail="Restoring boot volume into {}: {}".format(dest_ad, display_name))

    source = oci.core.models.BootVolumeSourceFromBootVolumeBackupDetails(id=boot_backup_id)
    details = oci.core.models.CreateBootVolumeDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=display_name,
        source_details=source
    )
    bv = block.create_boot_volume(details).data
    wait_for_state(lambda: block.get_boot_volume(bv.id).data, "AVAILABLE",
                   label="restored boot volume", progress=progress)
    if progress:
        progress.update(1.0, detail="Restored boot volume AVAILABLE")
    return bv.id


def restore_block_volumes(block, compartment_id: str, dest_ad: str, volume_backup_map: Dict[str, str], name_prefix: str,
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
            source_details=source
        )
        vol = block.create_volume(details).data
        wait_for_state(lambda: block.get_volume(vol.id).data, "AVAILABLE",
                       label="restored block volume {}/{}".format(i, total), progress=progress)
        restored[orig_vol_id] = vol.id

        if progress:
            progress.update(i / float(total), detail="Restored block volume {}/{} AVAILABLE".format(i, total))

    return restored


def create_new_instance(
    compute,
    instance,
    compartment_id: str,
    dest_ad: str,
    boot_volume_id: str,
    primary_vnic,
    ssh_key: Optional[str] = None,
    new_shape: Optional[str] = None,
    progress: Optional["StepProgress"] = None
) -> Tuple[str, str]:
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = "{}-Bulma-{}".format(instance.display_name, suffix)
    shape = new_shape if new_shape else instance.shape

    if progress:
        progress.update(0.05, detail="Launching NEW instance: {} (shape={})".format(new_name, shape))

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id=boot_volume_id)

    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

    assign_public_ip = bool(getattr(primary_vnic, "public_ip", None))
    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=primary_vnic.subnet_id,
        display_name=primary_vnic.display_name or "{}-vnic0".format(new_name),
        assign_public_ip=assign_public_ip,
        nsg_ids=list(primary_vnic.nsg_ids or [])
    )

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=new_name,
        shape=shape,
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        metadata=metadata
    )

    new_inst = compute.launch_instance(details).data
    wait_for_state(lambda: compute.get_instance(new_inst.id).data, "RUNNING",
                   label="new instance", progress=progress)

    if progress:
        progress.update(1.0, detail="New instance RUNNING")
    return new_inst.id, new_name


def attach_secondary_vnics(compute, instance_vnics, new_instance_id: str, progress: Optional["StepProgress"] = None) -> List[str]:
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
            instance_id=new_instance_id,
            create_vnic_details=create_vnic
        )
        va = compute.attach_vnic(details).data
        wait_for_state(lambda: compute.get_vnic_attachment(va.id).data, "ATTACHED",
                       label="vnic_attachment {}/{}".format(idx, total), progress=progress)

        created.append(va.id)
        if progress:
            progress.update(idx / float(total), detail="Secondary VNIC {}/{} ATTACHED".format(idx, total))

    return created


def attach_restored_volumes(
    compute,
    new_instance_id: str,
    original_attachments,
    restored_map: Dict[str, str],
    progress: Optional["StepProgress"] = None
) -> List[str]:
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
        wait_for_state(lambda: compute.get_volume_attachment(va.id).data, "ATTACHED",
                       label="volume_attachment {}/{}".format(i, total), progress=progress)

        attached.append(va.id)
        if progress:
            progress.update(i / float(total), detail="Volume {}/{} ATTACHED".format(i, total))

    return attached


# =============================================================================
# Main
# =============================================================================

def main():
    args = parse_args()

    config, signer = get_signer_and_config()
    identity, compute, network, block, lb, lb_ops = get_clients(config, signer)
    tenancy_id = get_tenancy_id(config, signer)

    compartment_id = prompt("Enter COMPARTMENT OCID (where the source instances exist)")
    if not compartment_id.startswith("ocid1.compartment"):
        print("Compartment OCID does not look right. Exiting.")
        sys.exit(1)

    lb_compartment_id = args.lb_compartment_id.strip() or compartment_id

    # Availability domains
    ads = identity.list_availability_domains(tenancy_id).data
    if not ads:
        print("No availability domains found. Exiting.")
        sys.exit(1)

    dest_ad_obj = choose_from_list("Select destination Availability Domain", ads, lambda ad: ad.name)
    dest_ad = dest_ad_obj.name

    # Optional: override shape
    new_shape = None
    if yn("Override shape for NEW instance(s)?", default="n"):
        new_shape = prompt("Enter new shape name (e.g., VM.Standard3.Flex)")

    # Optional SSH key
    ssh_key = None
    if yn("Provide/override SSH public key in metadata for NEW instances? (recommended for Linux)", default="y"):
        ssh_key = prompt("Paste SSH public key (single line)", default="").strip() or None

    # List instances
    instances = list_instances_in_compartment(compute, compartment_id)
    if not instances:
        print("No instances found in that compartment.")
        sys.exit(0)

    print("\nInstances")
    print("---------")
    for idx, inst in enumerate(instances, 1):
        print("{:>2}. {} | {} | {} | {}".format(idx, inst.display_name, inst.lifecycle_state, inst.availability_domain, inst.shape))

    chosen = choose_multi_indices(len(instances))
    selected = [instances[i] for i in chosen]

    disable_old = args.lb_disable_old
    if args.restore_lb and not args.lb_disable_old:
        disable_old = yn("LB detected backends: set OLD backend(s) to DRAIN+OFFLINE after adding NEW ones?", default="y")

    print("\nPlanned actions (per instance)")
    print("- Stop instance (shutdown)")
    print("- Backup boot + block volumes")
    print("- Restore volumes into destination AD")
    print("- Create new instance from restored boot volume (same subnet + NSGs; NEW private IPs)")
    print("- Attach restored block volumes")
    if args.restore_lb:
        print("- Detect classic LB backend membership and add NEW backend(s) back into the same backend set(s)")
        print("  - LB compartment: {}".format(lb_compartment_id))
        print("  - Disable OLD backends: {}".format(disable_old))
    if not yn("Continue?", default="y"):
        print("Canceled.")
        sys.exit(0)

    multi = MultiProgress(len(selected))
    report: List[Dict[str, Any]] = []

    for n, inst in enumerate(selected, start=1):
        steps = [
            ("Discover configuration", 8),
            ("Scan Load Balancers", 8 if args.restore_lb else 0),
            ("Stop source instance", 12),
            ("Backup boot volume", 12),
            ("Backup block volumes", 18),
            ("Restore boot volume", 12),
            ("Restore block volumes", 18),
            ("Launch new instance", 12),
            ("Attach secondary VNICs", 6),
            ("Attach block volumes", 10),
            ("Restore LB backends", 12 if args.restore_lb else 0),
        ]

        label = "Bulma | {}".format(inst.display_name)
        prog = StepProgress(label=label, steps=steps, overall_prefix=multi.overall_prefix(n))

        print("\n" + "=" * 90)
        print("{} | {} START instance migration: {} ({})".format(_ts(), multi.overall_prefix(n), inst.display_name, inst.id), flush=True)
        print("=" * 90)

        # Discover configuration
        prog.start_step("Discover configuration")
        inst = compute.get_instance(inst.id).data
        instance_vnics = get_instance_vnics(compute, network, compartment_id, inst.id)
        if not instance_vnics:
            raise RuntimeError("No VNIC attachments found.")
        _, primary_vnic = instance_vnics[0]

        boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, inst.id)
        block_attachments = get_instance_block_volume_attachments(compute, compartment_id, inst.id)
        prog.update(0.7, detail="VNICs={} | block_vols={} | boot_vol={}".format(
            len(instance_vnics), len(block_attachments), boot_volume_id[-12:]
        ))
        prog.complete_step("Configuration collected")

        # LB scan
        lb_matches: List[Dict[str, Any]] = []
        if args.restore_lb:
            prog.start_step("Scan Load Balancers")
            ip_to_subnet = get_ip_to_subnet_map_for_instance(network, instance_vnics)
            prog.update(0.05, detail="Instance IPs discovered: {} (matching against classic LBs)".format(len(ip_to_subnet)))
            lb_matches = discover_classic_lb_membership(lb, lb_compartment_id, ip_to_subnet, progress=prog)
            prog.complete_step("LB matches={}".format(len(lb_matches)))

        # Stop instance
        prog.start_step("Stop source instance")
        stop_instance_if_needed(compute, inst, progress=prog)
        prog.complete_step("Source instance STOPPED")

        # Backups
        name_prefix = inst.display_name.replace(" ", "_")[:40]

        prog.start_step("Backup boot volume")
        boot_bkp_id = backup_boot_volume(block, boot_volume_id, name_prefix, progress=prog)
        prog.complete_step("Boot backup complete")

        prog.start_step("Backup block volumes")
        blk_bkp_map = backup_block_volumes(block, block_attachments, name_prefix, progress=prog)
        prog.complete_step("Block backups complete (count={})".format(len(blk_bkp_map)))

        # Restores
        prog.start_step("Restore boot volume")
        new_boot_vol_id = restore_boot_volume(block, compartment_id, dest_ad, boot_bkp_id, name_prefix, progress=prog)
        prog.complete_step("Boot restore complete")

        prog.start_step("Restore block volumes")
        restored_blk_map = restore_block_volumes(block, compartment_id, dest_ad, blk_bkp_map, name_prefix, progress=prog)
        prog.complete_step("Block restore complete (count={})".format(len(restored_blk_map)))

        # Launch new instance
        prog.start_step("Launch new instance")
        new_instance_id, new_instance_name = create_new_instance(
            compute, inst, compartment_id, dest_ad, new_boot_vol_id, primary_vnic,
            ssh_key=ssh_key, new_shape=new_shape, progress=prog
        )
        prog.complete_step("New instance running")

        # Secondary VNICs
        prog.start_step("Attach secondary VNICs")
        created_vnic_attachment_ids = attach_secondary_vnics(compute, instance_vnics, new_instance_id, progress=prog)
        prog.complete_step("Secondary VNICs attached (count={})".format(len(created_vnic_attachment_ids)))

        # Attach volumes
        prog.start_step("Attach block volumes")
        created_volume_attachment_ids = attach_restored_volumes(
            compute, new_instance_id, block_attachments, restored_blk_map, progress=prog
        )
        prog.complete_step("Volumes attached (count={})".format(len(created_volume_attachment_ids)))

        # Determine new instance IPs (for LB restore)
        new_instance_vnics = get_instance_vnics(compute, network, compartment_id, new_instance_id)
        new_subnet_to_ip = get_subnet_to_primary_ip_for_instance(network, new_instance_vnics)
        new_primary_private_ip = new_subnet_to_ip.get(primary_vnic.subnet_id)

        # Restore LB membership
        lb_actions: List[Dict[str, Any]] = []
        if args.restore_lb:
            prog.start_step("Restore LB backends")
            if lb_matches:
                total_matches = len(lb_matches)
                for i, m in enumerate(lb_matches):
                    desired_ip = new_subnet_to_ip.get(m.get("subnet_id")) or new_primary_private_ip
                    if not desired_ip:
                        prog.update((i + 1) / float(total_matches), detail="Could not determine new IP; skipping backend restore")
                        continue
                    ensure_backend_in_classic_lb(
                        lb_ops, m, desired_ip, disable_old, progress=prog, idx=i, total=total_matches
                    )
                    lb_actions.append({
                        "lb_name": m["lb_name"],
                        "lb_id": m["lb_id"],
                        "backend_set": m["backend_set_name"],
                        "old_backend": "{}:{}".format(m["old_ip"], m["port"]),
                        "new_backend": "{}:{}".format(desired_ip, m["port"]),
                        "disabled_old": disable_old
                    })
                prog.complete_step("LB restored (actions={})".format(len(lb_actions)))
            else:
                prog.update(1.0, detail="No LB membership detected; nothing to restore")
                prog.complete_step("Skipped")

        print("\n{} | {} DONE ✅  New instance: {} ({})".format(_ts(), multi.overall_prefix(n), new_instance_name, new_instance_id), flush=True)
        print("{} | {} NOTE: Original instance remains STOPPED and otherwise unchanged.\n".format(_ts(), multi.overall_prefix(n)), flush=True)

        report.append({
            "source_instance": inst.display_name,
            "source_instance_id": inst.id,
            "source_ad": inst.availability_domain,
            "dest_ad": dest_ad,
            "new_instance": new_instance_name,
            "new_instance_id": new_instance_id,
            "new_primary_private_ip": new_primary_private_ip,
            "boot_backup_id": boot_bkp_id,
            "restored_boot_volume_id": new_boot_vol_id,
            "restored_block_volumes_count": len(restored_blk_map),
            "created_vnic_attachments": created_vnic_attachment_ids,
            "created_volume_attachments": created_volume_attachment_ids,
            "lb_actions": lb_actions
        })

    # Final report
    print("\n" + "#" * 90)
    print("{} | BULMA MIGRATION REPORT".format(_ts()))
    print("#" * 90)
    for r in report:
        print("\nSource: {} ({})".format(r["source_instance"], r["source_instance_id"]))
        print("  AD: {} -> {}".format(r["source_ad"], r["dest_ad"]))
        print("  New: {} ({})".format(r["new_instance"], r["new_instance_id"]))
        print("  New primary private IP: {}".format(r["new_primary_private_ip"]))
        if r["lb_actions"]:
            print("  LB restored:")
            for a in r["lb_actions"]:
                print("    - {} / {}: {} -> {} (old disabled={})".format(
                    a["lb_name"], a["backend_set"], a["old_backend"], a["new_backend"], a["disabled_old"]
                ))
        else:
            print("  LB restored: none / not detected")
    print("\nAll done. 🚀")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n{} | Canceled by user.".format(_ts()))
        sys.exit(130)
    except Exception as e:
        print("\n{} | ERROR: {}".format(_ts(), e))
        sys.exit(1)
