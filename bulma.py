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
            line += " — " + detail
        print(line, flush=True)


class MultiProgress:
    def __init__(self, total_instances: int):
        self.total = max(1, int(total_instances))

    def overall_prefix(self, idx_1based: int) -> str:
        return "[{}/{}]".format(idx_1based, self.total)


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="Bulma: AD-to-AD migration via backup/restore (keeps original instance).")
    p.add_argument("instance_ids", nargs="+", help="Instance OCID(s). Example: python3 bulma.py -- ocid1.instance...")
    p.add_argument("--dest-ad", default="", help="Optional destination AD name. If omitted, Bulma picks a different AD automatically.")
    p.add_argument("--shape", default="", help="Optional shape for NEW instance. If omitted, keeps original shape.")
    p.add_argument("--ssh-key", default="", help="Optional SSH public key to inject into metadata for NEW instance (Linux).")

    p.add_argument("--restore-lb", action="store_true", help="Detect classic LB membership and re-add new backend(s).")
    p.add_argument("--lb-disable-old", action="store_true", help="After adding new backend(s), set old backend(s) drain+offline.")
    p.add_argument("--lb-compartment-id", default="", help="Compartment OCID where LB(s) live (default: same as instance compartment).")

    p.add_argument("--wait", type=int, default=3600, help="Max wait per resource state change (seconds). Default 3600.")
    p.add_argument("--poll", type=int, default=10, help="Polling interval seconds. Default 10.")
    return p.parse_args()


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

def get_signer_and_config(region_hint: str):
    """
    Goal: ALWAYS return a config with region set.
    - Try Resource Principals (if available) -> signer provides region
    - Else use ~/.oci/config, but if region missing set from region_hint (from OCID)
    """
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        config = {"region": signer.region, "tenancy": getattr(signer, "tenancy_id", None)}
        print("{} | [Auth] Using Resource Principals signer (region={})".format(_ts(), config["region"]), flush=True)
        return config, signer
    except Exception:
        config = oci.config.from_file()
        signer = None

        # If region is missing in ~/.oci/config, infer from OCID
        if not config.get("region"):
            config["region"] = region_hint

        print("{} | [Auth] Using config file (~/.oci/config) (region={})".format(_ts(), config.get("region")), flush=True)

        if not config.get("region"):
            raise RuntimeError("No region in ~/.oci/config and could not infer region. Provide a valid instance OCID.")

        if not config.get("tenancy"):
            raise RuntimeError("No tenancy in ~/.oci/config. Add tenancy OCID to config or use Resource Principal auth.")

        return config, signer


def get_tenancy_id(config: Dict[str, Any], signer) -> str:
    if config.get("tenancy"):
        return config["tenancy"]
    if signer is not None and getattr(signer, "tenancy_id", None):
        return signer.tenancy_id
    raise RuntimeError("Tenancy OCID missing (cannot list Availability Domains).")


def get_clients(config: Dict[str, Any], signer=None):
    identity = oci.identity.IdentityClient(config, signer=signer)
    compute = oci.core.ComputeClient(config, signer=signer)
    network = oci.core.VirtualNetworkClient(config, signer=signer)
    block = oci.core.BlockstorageClient(config, signer=signer)
    lb = oci.load_balancer.LoadBalancerClient(config, signer=signer)
    lb_ops = oci.load_balancer.LoadBalancerClientCompositeOperations(lb)
    return identity, compute, network, block, lb, lb_ops


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
    ip_to_subnet: Dict[str, str] = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        for pip in privs:
            ip_to_subnet[pip.ip_address] = vnic.subnet_id
    return ip_to_subnet


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


# =============================================================================
# Classic Load Balancer discovery & restore
# =============================================================================

def discover_classic_lb_membership(lb_client, lb_compartment_id: str, instance_ip_to_subnet: Dict[str, str],
                                  progress: Optional["StepProgress"] = None) -> List[Dict[str, Any]]:
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
                        "max_connections": getattr(be, "max_connections", None),
                        "subnet_id": instance_ip_to_subnet.get(ip)
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

def stop_instance_if_needed(compute, instance, max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> None:
    if instance.lifecycle_state == "STOPPED":
        if progress:
            progress.update(1.0, detail="Instance already STOPPED")
        return
    if progress:
        progress.update(0.05, detail="Sending STOP action")
    compute.instance_action(instance.id, "STOP")
    wait_for_state(lambda: compute.get_instance(instance.id).data, "STOPPED", max_wait, poll, "instance", progress)
    if progress:
        progress.update(1.0, detail="Instance STOPPED")


def backup_boot_volume(block, boot_volume_id: str, name_prefix: str, max_wait: int, poll: int,
                       progress: Optional["StepProgress"] = None) -> str:
    display_name = "{}-boot-bkp-{}".format(name_prefix, datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    if progress:
        progress.update(0.05, detail="Creating boot backup: {}".format(display_name))
    details = oci.core.models.CreateBootVolumeBackupDetails(
        boot_volume_id=boot_volume_id,
        display_name=display_name,
        type="FULL"
    )
    bkp = block.create_boot_volume_backup(details).data
    wait_for_state(lambda: block.get_boot_volume_backup(bkp.id).data, "AVAILABLE", max_wait, poll, "boot backup", progress)
    if progress:
        progress.update(1.0, detail="Boot backup AVAILABLE")
    return bkp.id


def backup_block_volumes(block, volume_attachments, name_prefix: str, max_wait: int, poll: int,
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
            type="FULL"
        )
        bkp = block.create_volume_backup(details).data
        wait_for_state(lambda: block.get_volume_backup(bkp.id).data, "AVAILABLE", max_wait, poll,
                       "block backup {}/{}".format(i, total), progress)
        backup_map[vol_id] = bkp.id
        if progress:
            progress.update(i / float(total), detail="Block backup {}/{} AVAILABLE".format(i, total))

    return backup_map


def restore_boot_volume(block, compartment_id: str, dest_ad: str, boot_backup_id: str, name_prefix: str,
                        max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> str:
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
    wait_for_state(lambda: block.get_boot_volume(bv.id).data, "AVAILABLE", max_wait, poll, "restored boot volume", progress)
    if progress:
        progress.update(1.0, detail="Restored boot volume AVAILABLE")
    return bv.id


def restore_block_volumes(block, compartment_id: str, dest_ad: str, volume_backup_map: Dict[str, str], name_prefix: str,
                          max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> Dict[str, str]:
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
            progress.update((i - 1) / float(total), detail="Restoring block volume {}/{} into {}: {}".format(i, total, dest_ad, display_name))

        source = oci.core.models.VolumeSourceFromVolumeBackupDetails(id=backup_id)
        details = oci.core.models.CreateVolumeDetails(
            availability_domain=dest_ad,
            compartment_id=compartment_id,
            display_name=display_name,
            source_details=source
        )
        vol = block.create_volume(details).data
        wait_for_state(lambda: block.get_volume(vol.id).data, "AVAILABLE", max_wait, poll,
                       "restored block volume {}/{}".format(i, total), progress)
        restored[orig_vol_id] = vol.id
        if progress:
            progress.update(i / float(total), detail="Restored block volume {}/{} AVAILABLE".format(i, total))

    return restored


def create_new_instance(compute, instance, compartment_id: str, dest_ad: str, boot_volume_id: str, primary_vnic,
                        new_shape: str, ssh_key: str, max_wait: int, poll: int, progress: Optional["StepProgress"] = None) -> Tuple[str, str]:
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = "{}-Bulma-{}".format(instance.display_name, suffix)
    shape = new_shape if new_shape else instance.shape

    if progress:
        progress.update(0.05, detail="Launching NEW instance: {} (shape={})".format(new_name, shape))

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id=boot_volume_id)

    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

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
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        metadata=metadata
    )

    new_inst = compute.launch_instance(details).data
    wait_for_state(lambda: compute.get_instance(new_inst.id).data, "RUNNING", max_wait, poll, "new instance", progress)
    if progress:
        progress.update(1.0, detail="New instance RUNNING")
    return new_inst.id, new_name


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
            progress.update((idx - 1) / float(total), detail="Attaching secondary VNIC {}/{} (subnet+NSGs preserved)".format(idx, total))

        create_vnic = oci.core.models.CreateVnicDetails(
            subnet_id=vnic.subnet_id,
            display_name=vnic.display_name or "vnic{}".format(idx),
            assign_public_ip=bool(getattr(vnic, "public_ip", None)),
            nsg_ids=list(vnic.nsg_ids or [])
        )
        details = oci.core.models.CreateVnicAttachmentDetails(instance_id=new_instance_id, create_vnic_details=create_vnic)
        va = compute.attach_vnic(details).data
        wait_for_state(lambda: compute.get_vnic_attachment(va.id).data, "ATTACHED", max_wait, poll,
                       "vnic_attachment {}/{}".format(idx, total), progress)
        created.append(va.id)
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
            progress.update((i - 1) / float(total), detail="Attaching volume {}/{} (type={}, ro={})".format(i, total, atype, is_read_only))

        if atype == "ISCSI":
            details = oci.core.models.AttachIScsiVolumeDetails(
                instance_id=new_instance_id, volume_id=new_vol_id, is_read_only=is_read_only, display_name=getattr(att, "display_name", None)
            )
        else:
            details = oci.core.models.AttachParavirtualizedVolumeDetails(
                instance_id=new_instance_id, volume_id=new_vol_id, is_read_only=is_read_only, display_name=getattr(att, "display_name", None)
            )
            device = getattr(att, "device", None)
            if device:
                details.device = device

        va = compute.attach_volume(details).data
        wait_for_state(lambda: compute.get_volume_attachment(va.id).data, "ATTACHED", max_wait, poll,
                       "volume_attachment {}/{}".format(i, total), progress)
        attached.append(va.id)
        if progress:
            progress.update(i / float(total), detail="Volume {}/{} ATTACHED".format(i, total))

    return attached


# =============================================================================
# Main
# =============================================================================

def main():
    args = parse_args()

    # validate + infer region
    for ocid in args.instance_ids:
        if not ocid.startswith("ocid1.instance"):
            raise ValueError("Not an instance OCID: {}".format(ocid))

    regions = sorted(set(infer_region_from_instance_ocid(x) for x in args.instance_ids))
    if len(regions) != 1:
        raise ValueError("All instance OCIDs must be in the same region. Found regions: {}".format(regions))
    region_hint = regions[0]

    config, signer = get_signer_and_config(region_hint)
    identity, compute, network, block, lb, lb_ops = get_clients(config, signer)
    tenancy_id = get_tenancy_id(config, signer)

    multi = MultiProgress(len(args.instance_ids))

    for n, instance_id in enumerate(args.instance_ids, start=1):
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

        prog = StepProgress(label="Bulma | {}".format(instance_id[-10:]), steps=steps, overall_prefix=multi.overall_prefix(n))

        prog.start_step("Discover configuration")
        inst = compute.get_instance(instance_id).data
        prog.label = "Bulma | {}".format(inst.display_name)

        compartment_id = inst.compartment_id
        source_ad = inst.availability_domain
        dest_ad = pick_destination_ad(identity, tenancy_id, source_ad, args.dest_ad)

        instance_vnics = get_instance_vnics(compute, network, compartment_id, instance_id)
        _, primary_vnic = instance_vnics[0]

        boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, instance_id)
        block_attachments = get_instance_block_volume_attachments(compute, compartment_id, instance_id)

        prog.update(0.7, detail="region={} | sourceAD={} -> destAD={} | vnics={} | blockVols={}".format(
            config.get("region"), source_ad, dest_ad, len(instance_vnics), len(block_attachments)
        ))
        prog.complete_step("Configuration collected")

        # LB scan
        lb_matches: List[Dict[str, Any]] = []
        lb_compartment_id = args.lb_compartment_id.strip() or compartment_id
        if args.restore_lb:
            prog.start_step("Scan Load Balancers")
            ip_to_subnet = get_ip_to_subnet_map_for_instance(network, instance_vnics)
            prog.update(0.05, detail="scanning classic LBs in compartment {}".format(lb_compartment_id))
            lb_matches = discover_classic_lb_membership(lb, lb_compartment_id, ip_to_subnet, progress=prog)
            prog.complete_step("LB matches={}".format(len(lb_matches)))

        # Stop
        prog.start_step("Stop source instance")
        stop_instance_if_needed(compute, inst, args.wait, args.poll, progress=prog)
        prog.complete_step("Source instance STOPPED")

        name_prefix = inst.display_name.replace(" ", "_")[:40]

        # Backup boot
        prog.start_step("Backup boot volume")
        boot_bkp_id = backup_boot_volume(block, boot_volume_id, name_prefix, args.wait, args.poll, progress=prog)
        prog.complete_step("Boot backup complete")

        # Backup block
        prog.start_step("Backup block volumes")
        blk_bkp_map = backup_block_volumes(block, block_attachments, name_prefix, args.wait, args.poll, progress=prog)
        prog.complete_step("Block backups complete (count={})".format(len(blk_bkp_map)))

        # Restore boot
        prog.start_step("Restore boot volume")
        new_boot_vol_id = restore_boot_volume(block, compartment_id, dest_ad, boot_bkp_id, name_prefix, args.wait, args.poll, progress=prog)
        prog.complete_step("Boot restore complete")

        # Restore block
        prog.start_step("Restore block volumes")
        restored_blk_map = restore_block_volumes(block, compartment_id, dest_ad, blk_bkp_map, name_prefix, args.wait, args.poll, progress=prog)
        prog.complete_step("Block restore complete (count={})".format(len(restored_blk_map)))

        # Launch new instance
        prog.start_step("Launch new instance")
        new_instance_id, new_instance_name = create_new_instance(
            compute, inst, compartment_id, dest_ad, new_boot_vol_id, primary_vnic,
            args.shape, args.ssh_key, args.wait, args.poll, progress=prog
        )
        prog.complete_step("New instance running")

        # Secondary VNICs
        prog.start_step("Attach secondary VNICs")
        attach_secondary_vnics(compute, instance_vnics, new_instance_id, args.wait, args.poll, progress=prog)
        prog.complete_step("Secondary VNICs attached")

        # Attach volumes
        prog.start_step("Attach block volumes")
        attach_restored_volumes(compute, new_instance_id, block_attachments, restored_blk_map, args.wait, args.poll, progress=prog)
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
                        prog.update((i + 1) / float(total), detail="Could not determine new IP; skipping backend restore")
                        continue
                    ensure_backend_in_classic_lb(lb_ops, m, desired_ip, args.lb_disable_old, progress=prog, idx=i, total=total)

                prog.complete_step("LB restored")
            else:
                prog.update(1.0, detail="No LB membership detected; nothing to restore")
                prog.complete_step("Skipped")


if __name__ == "__main__":
    try:
        main()
        print("\n{} | All done. 🚀".format(_ts()))
    except KeyboardInterrupt:
        print("\n{} | Canceled by user.".format(_ts()))
        sys.exit(130)
    except Exception as e:
        print("\n{} | ERROR: {}".format(_ts(), e))
        sys.exit(1)
