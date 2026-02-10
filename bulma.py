#!/usr/bin/env python3
"""
bulma.py — OCI AD-to-AD Compute migration via Backup & Restore (keeps original instance)

What it does (safe mode):
- User selects 1+ instances in a compartment
- Shuts down selected instance(s) (for consistent backups)
- Creates backups for boot + attached block volumes
- Restores volumes into destination Availability Domain (same region)
- Launches NEW instance from restored boot volume
- Recreates VNIC attachments (subnet + NSGs) for primary + secondary VNICs
- Reattaches restored block volumes
- OPTIONAL: Detect classic Load Balancer membership and re-add NEW backend(s) to the same backend set(s)

NEW (per your request):
- Highly visible progress:
  - Overall progress 0–100%
  - Current step progress 0–100%
  - Step name + live details (counts, states, work request status)
  - Per-instance and overall (when multiple instances are selected)

Run:
  chmod +x bulma.py
  ./bulma.py --restore-lb
Optional:
  ./bulma.py --restore-lb --lb-disable-old
  ./bulma.py --restore-lb --lb-compartment-id ocid1.compartment...

Notes:
- Classic Load Balancer (LBaaS) backends are IP:port. We match by *any private IP* on *any VNIC* of the source instance.
- If you use Network Load Balancer (NLB), not handled here yet.
"""

import argparse
import sys
import time
from datetime import datetime

import oci
from oci.pagination import list_call_get_all_results


# =============================================================================
# Progress / Visibility (0–100%)
# =============================================================================

def _ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")


def _bar(pct: int, width: int = 28) -> str:
    pct = max(0, min(100, pct))
    filled = int(round(width * (pct / 100)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


class StepProgress:
    """
    Tracks one unit of work (e.g., one instance migration) with weighted steps.
    Prints:
      - overall 0–100%
      - step 0–100%
      - step label + details
    """
    def __init__(self, label: str, steps: list[tuple[str, int]], overall_prefix: str = ""):
        # steps: [(name, weight), ...] weights sum to 100 (we normalize if needed)
        self.label = label
        self.overall_prefix = overall_prefix
        self.steps = self._normalize(steps)
        self._idx = -1
        self._completed_weight = 0
        self._current_step_name = ""
        self._current_step_weight = 0

    @staticmethod
    def _normalize(steps: list[tuple[str, int]]) -> list[tuple[str, int]]:
        # Remove zero/negative weights
        s = [(n, int(w)) for n, w in steps if int(w) > 0]
        total = sum(w for _, w in s)
        if total == 0:
            return [("Work", 100)]
        # If already 100, keep. Otherwise scale to 100.
        if total == 100:
            return s

        scaled = []
        acc = 0
        for i, (n, w) in enumerate(s):
            if i == len(s) - 1:
                sw = 100 - acc
            else:
                sw = max(1, int(round((w / total) * 100)))
                # avoid overshooting; keep room for remaining
                if acc + sw > 99:
                    sw = max(1, 99 - acc)
            acc += sw
            scaled.append((n, sw))
        # Fix any rounding drift
        drift = 100 - sum(w for _, w in scaled)
        if drift != 0:
            n, w = scaled[-1]
            scaled[-1] = (n, max(1, w + drift))
        return scaled

    def start_step(self, name: str):
        # Move to step by name (in order)
        for i in range(self._idx + 1, len(self.steps)):
            if self.steps[i][0] == name:
                self._idx = i
                self._current_step_name = name
                self._current_step_weight = self.steps[i][1]
                self._print(overall_pct=self._completed_weight, step_pct=0, detail="START")
                return
        raise ValueError(f"Step '{name}' not found in steps list.")

    def update(self, step_frac: float, detail: str = ""):
        step_frac = max(0.0, min(1.0, step_frac))
        step_pct = int(round(step_frac * 100))
        overall = int(round(self._completed_weight + (step_frac * self._current_step_weight)))
        self._print(overall_pct=overall, step_pct=step_pct, detail=detail)

    def complete_step(self, detail: str = "DONE"):
        # Mark current step fully complete
        self._print(overall_pct=self._completed_weight + self._current_step_weight, step_pct=100, detail=detail)
        self._completed_weight += self._current_step_weight
        self._current_step_weight = 0
        self._current_step_name = ""

    def _print(self, overall_pct: int, step_pct: int, detail: str):
        prefix = f"{self.overall_prefix} " if self.overall_prefix else ""
        line = (
            f"{_ts()} | {prefix}{self.label} | "
            f"Overall {overall_pct:>3}% {_bar(overall_pct)} | "
            f"Step {step_pct:>3}% {_bar(step_pct, 18)} | "
            f"{self._current_step_name}"
        )
        if detail:
            line += f" — {detail}"
        print(line, flush=True)


class MultiProgress:
    """
    Adds an overall progress across multiple instances by mapping each instance progress into total.
    """
    def __init__(self, total_instances: int):
        self.total = max(1, total_instances)

    def overall_prefix(self, instance_index_1based: int) -> str:
        return f"[{instance_index_1based}/{self.total}]"


# =============================================================================
# Auth / client setup
# =============================================================================

def get_signer_and_config():
    # Try Resource Principal first; otherwise ~/.oci/config
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        config = {"region": signer.region, "tenancy": getattr(signer, "tenancy_id", None)}
        print(f"{_ts()} | [Auth] Using Resource Principals signer (region={config['region']})", flush=True)
        return config, signer
    except Exception:
        config = oci.config.from_file()
        signer = None
        print(f"{_ts()} | [Auth] Using config file (~/.oci/config) (region={config.get('region')})", flush=True)
        return config, signer


def get_tenancy_id(config, signer):
    if config.get("tenancy"):
        return config["tenancy"]
    if signer is not None and getattr(signer, "tenancy_id", None):
        return signer.tenancy_id
    tid = input("Enter TENANCY OCID (required to list Availability Domains): ").strip()
    if not tid.startswith("ocid1.tenancy"):
        raise ValueError("Tenancy OCID does not look right.")
    return tid


def get_clients(config, signer=None):
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


def prompt(msg, default=None):
    if default is not None:
        val = input(f"{msg} [{default}]: ").strip()
        return val if val else default
    return input(f"{msg}: ").strip()


def yn(msg, default="y"):
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


def choose_from_list(title, items, render_fn):
    print("\n" + title)
    print("-" * len(title))
    for idx, it in enumerate(items, 1):
        print(f"{idx:>2}. {render_fn(it)}")
    while True:
        raw = input("Select number: ").strip()
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(items):
                return items[n - 1]
        print("Invalid selection, try again.")


def choose_multi_indices(items_len):
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


def wait_for_state(getter_fn, desired_state, max_wait_seconds=3600, interval=10, label="resource", progress: StepProgress | None = None):
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
                progress.update(min(0.95, (time.time() - start) / max_wait_seconds), detail=f"{label} state={state} (waiting for {desired_state})")
            else:
                print(f"{_ts()} | - waiting for {label}: {state} -> {desired_state} ...", flush=True)

        if state == desired_state:
            if progress:
                progress.update(0.99, detail=f"{label} reached {desired_state}")
            return obj

        if time.time() - start > max_wait_seconds:
            raise TimeoutError(f"Timed out waiting for {label} to reach {desired_state}. Last state: {state}")

        # periodic progress pulse
        if progress:
            progress.update(min(0.95, (time.time() - start) / max_wait_seconds), detail=f"{label} state={state} (waiting...)")

        time.sleep(interval)


# =============================================================================
# OCI discovery
# =============================================================================

def list_instances_in_compartment(compute, compartment_id):
    resp = list_call_get_all_results(
        compute.list_instances,
        compartment_id=compartment_id,
        sort_by="DISPLAYNAME",
        sort_order="ASC"
    )
    return [i for i in resp.data if i.lifecycle_state != "TERMINATED"]


def get_instance_vnics(compute, network, compartment_id, instance_id):
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


def get_instance_boot_volume_id(compute, compartment_id, instance_id):
    bvas = list_call_get_all_results(
        compute.list_boot_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data
    if not bvas:
        raise RuntimeError("No boot volume attachment found.")
    return bvas[0].boot_volume_id


def get_instance_block_volume_attachments(compute, compartment_id, instance_id):
    return list_call_get_all_results(
        compute.list_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data


def get_ip_to_subnet_map_for_instance(network, instance_vnics):
    """
    dict[ip_address] = subnet_id for ALL private IPs on ALL VNICs
    """
    ip_to_subnet = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        for pip in privs:
            ip_to_subnet[pip.ip_address] = vnic.subnet_id
    return ip_to_subnet


def get_subnet_to_primary_ip_for_instance(network, instance_vnics):
    """
    dict[subnet_id] = primary_private_ip for each VNIC/subnet
    """
    subnet_to_ip = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        primary = next((p for p in privs if getattr(p, "is_primary", False)), None)
        if primary:
            subnet_to_ip[vnic.subnet_id] = primary.ip_address
    return subnet_to_ip


# =============================================================================
# Classic Load Balancer discovery & restore
# =============================================================================

def discover_classic_lb_membership(lb_client, lb_compartment_id, instance_ip_to_subnet, progress: StepProgress | None = None):
    """
    Finds classic LB backend entries that match any instance IP.
    Returns list of dicts:
      { lb_id, lb_name, backend_set_name, old_ip, port, weight, backup, drain, offline, max_connections, subnet_id }
    """
    instance_ips = set(instance_ip_to_subnet.keys())
    matches = []

    lbs = list_call_get_all_results(lb_client.list_load_balancers, compartment_id=lb_compartment_id).data
    total_lbs = max(1, len(lbs))

    for i, lb in enumerate(lbs, start=1):
        if progress:
            progress.update((i - 1) / total_lbs, detail=f"Scanning LB {i}/{len(lbs)}: {getattr(lb, 'display_name', lb.id)}")
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
        progress.update(0.99, detail=f"LB scan done. Matches found: {len(matches)}")

    return matches


def ensure_backend_in_classic_lb(lb_ops, match, new_ip, disable_old, progress: StepProgress | None = None, idx: int = 0, total: int = 1):
    """
    Adds/updates backend in classic LB backend set; optionally disables old backend.
    Waits for work request SUCCEEDED using composite operations.
    """
    lb_id = match["lb_id"]
    bs = match["backend_set_name"]
    port = match["port"]

    if progress:
        progress.update((idx / max(1, total)) * 0.7, detail=f"Adding backend {new_ip}:{port} to {match['lb_name']}/{bs} ({idx+1}/{total})")

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
        # Often means already exists; attempt update
        if e.status in (400, 409):
            backend_name = f"{new_ip}:{port}"
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
            progress.update((idx / max(1, total)) * 0.7 + 0.2, detail=f"Disabling OLD backend {match['old_ip']}:{port} (DRAIN+OFFLINE)")
        old_backend_name = f"{match['old_ip']}:{port}"
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
        progress.update(((idx + 1) / max(1, total)) * 0.95, detail=f"LB backend updated: {match['lb_name']}/{bs}")


# =============================================================================
# Migration steps
# =============================================================================

def stop_instance_if_needed(compute, instance, progress: StepProgress | None = None):
    if instance.lifecycle_state == "STOPPED":
        if progress:
            progress.update(1.0, detail="Instance already STOPPED")
        return
    if progress:
        progress.update(0.05, detail="Sending STOP action")
    compute.instance_action(instance.id, "STOP")
    wait_for_state(lambda: compute.get_instance(instance.id).data, "STOPPED", label="instance", progress=progress)


def backup_boot_volume(block, boot_volume_id, name_prefix, progress: StepProgress | None = None):
    display_name = f"{name_prefix}-boot-bkp-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    if progress:
        progress.update(0.05, detail=f"Creating boot backup: {display_name}")
    details = oci.core.models.CreateBootVolumeBackupDetails(
        boot_volume_id=boot_volume_id,
        display_name=display_name,
        type="FULL"
    )
    bkp = block.create_boot_volume_backup(details).data
    wait_for_state(lambda: block.get_boot_volume_backup(bkp.id).data, "AVAILABLE", label="boot backup", progress=progress)
    if progress:
        progress.update(1.0, detail="Boot backup AVAILABLE")
    return bkp.id


def backup_block_volumes(block, volume_attachments, name_prefix, progress: StepProgress | None = None):
    backup_map = {}  # volume_id -> backup_id
    total = len(volume_attachments)
    if total == 0:
        if progress:
            progress.update(1.0, detail="No attached block volumes to backup")
        return backup_map

    for i, att in enumerate(volume_attachments, start=1):
        vol_id = att.volume_id
        display_name = f"{name_prefix}-blk-bkp-{vol_id[-6:]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        if progress:
            progress.update((i - 1) / total, detail=f"Creating block backup {i}/{total}: {display_name}")

        details = oci.core.models.CreateVolumeBackupDetails(
            volume_id=vol_id,
            display_name=display_name,
            type="FULL"
        )
        bkp = block.create_volume_backup(details).data
        wait_for_state(lambda: block.get_volume_backup(bkp.id).data, "AVAILABLE", label=f"block backup {i}/{total}", progress=progress)
        backup_map[vol_id] = bkp.id

        if progress:
            progress.update(i / total, detail=f"Block backup {i}/{total} AVAILABLE")

    return backup_map


def restore_boot_volume(block, compartment_id, dest_ad, boot_backup_id, name_prefix, progress: StepProgress | None = None):
    display_name = f"{name_prefix}-boot-restored"
    if progress:
        progress.update(0.05, detail=f"Restoring boot volume into {dest_ad}: {display_name}")

    source = oci.core.models.BootVolumeSourceFromBootVolumeBackupDetails(id=boot_backup_id)
    details = oci.core.models.CreateBootVolumeDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=display_name,
        source_details=source
    )
    bv = block.create_boot_volume(details).data
    wait_for_state(lambda: block.get_boot_volume(bv.id).data, "AVAILABLE", label="restored boot volume", progress=progress)
    if progress:
        progress.update(1.0, detail="Restored boot volume AVAILABLE")
    return bv.id


def restore_block_volumes(block, compartment_id, dest_ad, volume_backup_map, name_prefix, progress: StepProgress | None = None):
    restored = {}  # original_volume_id -> new_volume_id
    total = len(volume_backup_map)
    if total == 0:
        if progress:
            progress.update(1.0, detail="No block volumes to restore")
        return restored

    for i, (orig_vol_id, backup_id) in enumerate(volume_backup_map.items(), start=1):
        display_name = f"{name_prefix}-blk-restored-{orig_vol_id[-6:]}"
        if progress:
            progress.update((i - 1) / total, detail=f"Restoring block volume {i}/{total} into {dest_ad}: {display_name}")

        source = oci.core.models.VolumeSourceFromVolumeBackupDetails(id=backup_id)
        details = oci.core.models.CreateVolumeDetails(
            availability_domain=dest_ad,
            compartment_id=compartment_id,
            display_name=display_name,
            source_details=source
        )
        vol = block.create_volume(details).data
        wait_for_state(lambda: block.get_volume(vol.id).data, "AVAILABLE", label=f"restored block volume {i}/{total}", progress=progress)
        restored[orig_vol_id] = vol.id

        if progress:
            progress.update(i / total, detail=f"Restored block volume {i}/{total} AVAILABLE")

    return restored


def create_new_instance(compute, instance, compartment_id, dest_ad, boot_volume_id, primary_vnic, ssh_key=None, new_shape=None, progress: StepProgress | None = None):
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = f"{instance.display_name}-Bulma-{suffix}"
    shape = new_shape if new_shape else instance.shape

    if progress:
        progress.update(0.05, detail=f"Launching NEW instance: {new_name} (shape={shape})")

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id=boot_volume_id)

    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

    assign_public_ip = bool(getattr(primary_vnic, "public_ip", None))
    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=primary_vnic.subnet_id,
        display_name=primary_vnic.display_name or f"{new_name}-vnic0",
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

    new_instance = compute.launch_instance(details).data
    wait_for_state(lambda: compute.get_instance(new_instance.id).data, "RUNNING", label="new instance", progress=progress)

    if progress:
        progress.update(1.0, detail="New instance RUNNING")
    return new_instance.id, new_name


def attach_secondary_vnics(compute, instance_vnics, new_instance_id, progress: StepProgress | None = None):
    if len(instance_vnics) <= 1:
        if progress:
            progress.update(1.0, detail="No secondary VNICs to recreate")
        return []

    created = []
    total = len(instance_vnics) - 1
    for idx, (_, vnic) in enumerate(instance_vnics[1:], start=1):
        if progress:
            progress.update((idx - 1) / total, detail=f"Attaching secondary VNIC {idx}/{total} (subnet+NSGs preserved)")

        create_vnic = oci.core.models.CreateVnicDetails(
            subnet_id=vnic.subnet_id,
            display_name=vnic.display_name or f"vnic{idx}",
            assign_public_ip=bool(getattr(vnic, "public_ip", None)),
            nsg_ids=list(vnic.nsg_ids or [])
        )
        details = oci.core.models.CreateVnicAttachmentDetails(
            instance_id=new_instance_id,
            create_vnic_details=create_vnic
        )
        va = compute.attach_vnic(details).data
        wait_for_state(lambda: compute.get_vnic_attachment(va.id).data, "ATTACHED", label=f"vnic_attachment {idx}/{total}", progress=progress)
        created.append(va.id)

        if progress:
            progress.update(idx / total, detail=f"Secondary VNIC {idx}/{total} ATTACHED")

    return created


def attach_restored_volumes(compute, new_instance_id, original_attachments, restored_map, progress: StepProgress | None = None):
    attached = []
    total = len([a for a in original_attachments if restored_map.get(a.volume_id)])
    if total == 0:
        if progress:
            progress.update(1.0, detail="No restored block volumes to attach")
        return attached

    j = 0
    for att in original_attachments:
        new_vol_id = restored_map.get(att.volume_id)
        if not new_vol_id:
            continue

        j += 1
        atype = (att.attachment_type or "").upper()
        is_read_only = bool(getattr(att, "is_read_only", False))
        if progress:
            progress.update((j - 1) / total, detail=f"Attaching volume {j}/{total} (type={atype}, ro={is_read_only})")

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
        wait_for_state(lambda: compute.get_volume_attachment(va.id).data, "ATTACHED", label=f"volume_attachment {j}/{total}", progress=progress)
        attached.append(va.id)

        if progress:
            progress.update(j / total, detail=f"Volume {j}/{total} ATTACHED")

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
        print(f"{idx:>2}. {inst.display_name} | {inst.lifecycle_state} | {inst.availability_domain} | {inst.shape}")

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
        print(f"  - LB compartment: {lb_compartment_id}")
        print(f"  - Disable OLD backends: {disable_old}")
    if not yn("Continue?", default="y"):
        print("Canceled.")
        sys.exit(0)

    multi = MultiProgress(len(selected))
    report = []

    for n, inst in enumerate(selected, start=1):
        # Weighted steps (normalize to 100 internally)
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

        label = f"Bulma | {inst.display_name}"
        prog = StepProgress(label=label, steps=steps, overall_prefix=multi.overall_prefix(n))

        print("\n" + "=" * 90)
        print(f"{_ts()} | {multi.overall_prefix(n)} START instance migration: {inst.display_name} ({inst.id})", flush=True)
        print("=" * 90)

        # Refresh
        prog.start_step("Discover configuration")
        inst = compute.get_instance(inst.id).data
        instance_vnics = get_instance_vnics(compute, network, compartment_id, inst.id)
        if not instance_vnics:
            raise RuntimeError("No VNIC attachments found.")
        primary_att, primary_vnic = instance_vnics[0]

        boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, inst.id)
        block_attachments = get_instance_block_volume_attachment_
