#!/usr/bin/env python3
"""
bulma.py — OCI AD-to-AD Compute “Backup & Restore” migration (safe: original instance NOT deleted)

NEW (per your request):
- Detect if the source instance is a backend in an OCI *Load Balancer* (classic LB service)
- After the new instance is created, add the new instance IP:port back into the same LB backend set(s)
- Optionally set the OLD backend(s) to OFFLINE+DRAIN (LB config change; does NOT modify the original server)

Notes:
- Classic Load Balancer backends are IP:port. This script matches by IP (any private IP on any VNIC).
- If your environment uses Network Load Balancer (NLB), this script does NOT handle it yet.

Run:
  chmod +x bulma.py
  ./bulma.py --restore-lb
Optional:
  ./bulma.py --restore-lb --lb-disable-old
  ./bulma.py --restore-lb --lb-compartment-id ocid1.compartment... (if LB is in different compartment)
"""

import argparse
import sys
import time
from datetime import datetime

import oci
from oci.pagination import list_call_get_all_results


# --------------------------
# Auth / client setup
# --------------------------

def get_signer_and_config():
    # Try Resource Principal first (works in many OCI-managed environments), otherwise ~/.oci/config
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        config = {"region": signer.region, "tenancy": getattr(signer, "tenancy_id", None)}
        print(f"[Auth] Using Resource Principals signer (region={config['region']})")
        return config, signer
    except Exception:
        config = oci.config.from_file()
        signer = None
        print(f"[Auth] Using config file (~/.oci/config) (region={config.get('region')})")
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


# --------------------------
# CLI
# --------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Bulma: AD-to-AD migration via backup/restore (keeps original instance).")
    p.add_argument("--restore-lb", action="store_true", help="Detect classic Load Balancer membership and re-add new backend(s).")
    p.add_argument("--lb-disable-old", action="store_true", help="After adding new backend(s), set old backend(s) drain+offline.")
    p.add_argument("--lb-compartment-id", default="", help="Compartment OCID where the Load Balancer(s) live (default: same as instance compartment).")
    return p.parse_args()


# --------------------------
# Helpers
# --------------------------

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


def wait_for_state(getter_fn, desired_state, max_wait_seconds=3600, interval=10, label="resource"):
    start = time.time()
    while True:
        obj = getter_fn()
        state = getattr(obj, "lifecycle_state", None)
        if state == desired_state:
            return obj
        if time.time() - start > max_wait_seconds:
            raise TimeoutError(f"Timed out waiting for {label} to reach {desired_state}. Last state: {state}")
        print(f"  - waiting for {label}: {state} -> {desired_state} ...")
        time.sleep(interval)


# --------------------------
# OCI discovery
# --------------------------

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
    Returns: dict[ip_address] = subnet_id for ALL private IPs on ALL VNICs (primary + secondary IPs).
    """
    ip_to_subnet = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        for pip in privs:
            ip_to_subnet[pip.ip_address] = vnic.subnet_id
    return ip_to_subnet


def get_subnet_to_primary_ip_for_instance(network, instance_vnics):
    """
    Returns: dict[subnet_id] = primary_private_ip (for each VNIC/subnet)
    """
    subnet_to_ip = {}
    for _, vnic in instance_vnics:
        privs = list_call_get_all_results(network.list_private_ips, vnic_id=vnic.id).data
        primary = next((p for p in privs if getattr(p, "is_primary", False)), None)
        if primary:
            subnet_to_ip[vnic.subnet_id] = primary.ip_address
    return subnet_to_ip


# --------------------------
# Load Balancer (classic) discovery & restore
# --------------------------

def discover_classic_lb_membership(lb_client, lb_compartment_id, instance_ip_to_subnet):
    """
    Finds classic LB backend entries that match any of the instance IPs.
    Returns list of dicts:
      { lb_id, lb_name, backend_set_name, old_ip, port, weight, backup, drain, offline, max_connections, subnet_id }
    """
    instance_ips = set(instance_ip_to_subnet.keys())
    matches = []

    lbs = list_call_get_all_results(lb_client.list_load_balancers, compartment_id=lb_compartment_id).data
    for lb in lbs:
        try:
            backend_sets = list_call_get_all_results(lb_client.list_backend_sets, load_balancer_id=lb.id).data
        except Exception:
            continue

        for bs in backend_sets:
            bs_name = getattr(bs, "name", None)
            if not bs_name:
                continue
            backends = list_call_get_all_results(
                lb_client.list_backends,
                load_balancer_id=lb.id,
                backend_set_name=bs_name
            ).data

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
    return matches


def ensure_backend_in_classic_lb(lb_ops, lb_client, match, new_ip, disable_old):
    """
    Adds/updates backend in classic LB backend set; optionally disables old backend.
    Uses CompositeOperations to wait for work request SUCCEEDED.
    """
    lb_id = match["lb_id"]
    bs = match["backend_set_name"]
    port = match["port"]

    # Add new backend (or update if already exists)
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
        print(f"  - LB: added backend {new_ip}:{port} to {match['lb_name']} / {bs}")
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
            print(f"  - LB: updated backend {backend_name} in {match['lb_name']} / {bs}")
        else:
            raise

    # Optionally disable old backend
    if disable_old:
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
        print(f"  - LB: set OLD backend {old_backend_name} to DRAIN+OFFLINE")


# --------------------------
# Migration steps
# --------------------------

def stop_instance_if_needed(compute, instance):
    if instance.lifecycle_state == "STOPPED":
        print(f"[Compute] Instance already STOPPED: {instance.display_name}")
        return
    print(f"[Compute] Stopping instance: {instance.display_name} ({instance.id})")
    compute.instance_action(instance.id, "STOP")
    wait_for_state(lambda: compute.get_instance(instance.id).data, "STOPPED", label="instance")


def backup_boot_volume(block, boot_volume_id, name_prefix):
    display_name = f"{name_prefix}-boot-bkp-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    print(f"[Block] Creating boot volume backup: {display_name}")
    details = oci.core.models.CreateBootVolumeBackupDetails(
        boot_volume_id=boot_volume_id,
        display_name=display_name,
        type="FULL"
    )
    bkp = block.create_boot_volume_backup(details).data
    wait_for_state(lambda: block.get_boot_volume_backup(bkp.id).data, "AVAILABLE", label="boot volume backup")
    return bkp.id


def backup_block_volumes(block, volume_attachments, name_prefix):
    backup_map = {}  # volume_id -> backup_id
    for att in volume_attachments:
        vol_id = att.volume_id
        display_name = f"{name_prefix}-blk-bkp-{vol_id[-6:]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        print(f"[Block] Creating block volume backup: {display_name}")
        details = oci.core.models.CreateVolumeBackupDetails(
            volume_id=vol_id,
            display_name=display_name,
            type="FULL"
        )
        bkp = block.create_volume_backup(details).data
        wait_for_state(lambda: block.get_volume_backup(bkp.id).data, "AVAILABLE", label="block volume backup")
        backup_map[vol_id] = bkp.id
    return backup_map


def restore_boot_volume(block, compartment_id, dest_ad, boot_backup_id, name_prefix):
    display_name = f"{name_prefix}-boot-restored"
    print(f"[Block] Restoring boot volume in {dest_ad}: {display_name}")
    source = oci.core.models.BootVolumeSourceFromBootVolumeBackupDetails(id=boot_backup_id)
    details = oci.core.models.CreateBootVolumeDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=display_name,
        source_details=source
    )
    bv = block.create_boot_volume(details).data
    wait_for_state(lambda: block.get_boot_volume(bv.id).data, "AVAILABLE", label="restored boot volume")
    return bv.id


def restore_block_volumes(block, compartment_id, dest_ad, volume_backup_map, name_prefix):
    restored = {}  # original_volume_id -> new_volume_id
    for orig_vol_id, backup_id in volume_backup_map.items():
        display_name = f"{name_prefix}-blk-restored-{orig_vol_id[-6:]}"
        print(f"[Block] Restoring block volume in {dest_ad}: {display_name}")
        source = oci.core.models.VolumeSourceFromVolumeBackupDetails(id=backup_id)
        details = oci.core.models.CreateVolumeDetails(
            availability_domain=dest_ad,
            compartment_id=compartment_id,
            display_name=display_name,
            source_details=source
        )
        vol = block.create_volume(details).data
        wait_for_state(lambda: block.get_volume(vol.id).data, "AVAILABLE", label="restored block volume")
        restored[orig_vol_id] = vol.id
    return restored


def create_new_instance(compute, instance, compartment_id, dest_ad, boot_volume_id, primary_vnic, ssh_key=None, new_shape=None):
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = f"{instance.display_name}-Bulma-{suffix}"
    shape = new_shape if new_shape else instance.shape

    print(f"[Compute] Creating new instance in {dest_ad}: {new_name} (shape={shape})")

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id=boot_volume_id)

    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

    # Preserve subnet + NSGs; allocate NEW private IPs (safe; original instance still exists)
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
    wait_for_state(lambda: compute.get_instance(new_instance.id).data, "RUNNING", label="new instance")
    return new_instance.id, new_name


def attach_secondary_vnics(compute, instance_vnics, new_instance_id):
    if len(instance_vnics) <= 1:
        return []

    created = []
    for idx, (_, vnic) in enumerate(instance_vnics[1:], start=1):
        print(f"[Network] Creating secondary VNIC attachment #{idx} (subnet+NSGs preserved, new IP allocated)")
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
        wait_for_state(lambda: compute.get_vnic_attachment(va.id).data, "ATTACHED", label=f"vnic_attachment_{idx}")
        created.append(va.id)
    return created


def attach_restored_volumes(compute, new_instance_id, original_attachments, restored_map):
    attached = []
    for att in original_attachments:
        new_vol_id = restored_map.get(att.volume_id)
        if not new_vol_id:
            continue

        atype = (att.attachment_type or "").upper()
        is_read_only = bool(getattr(att, "is_read_only", False))

        print(f"[Compute] Attaching restored volume {new_vol_id} (from {att.volume_id}) type={atype}")

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
        wait_for_state(lambda: compute.get_volume_attachment(va.id).data, "ATTACHED", label="volume_attachment")
        attached.append(va.id)

    return attached


# --------------------------
# Main
# --------------------------

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
        # Default behavior: ask once
        disable_old = yn("LB detected backends: set OLD backend(s) to DRAIN+OFFLINE after adding NEW ones?", default="y")

    print("\nPlanned actions (per instance)")
    print("- Stop instance (shutdown)")
    print("- Backup boot + block volumes")
    print("- Restore volumes into destination AD")
    print("- Create new instance from restored boot volume (same subnet + NSGs; new private IPs)")
    print("- Attach restored block volumes")
    if args.restore_lb:
        print("- Detect classic LB backend membership and add NEW backend(s) back into the same backend set(s)")
        print(f"  - LB compartment: {lb_compartment_id}")
        print(f"  - Disable OLD backends: {disable_old}")
    if not yn("Continue?", default="y"):
        print("Canceled.")
        sys.exit(0)

    report = []

    for inst in selected:
        print("\n" + "=" * 80)
        print(f"[Bulma] Migrating: {inst.display_name} ({inst.id})")
        print("=" * 80)

        inst = compute.get_instance(inst.id).data

        instance_vnics = get_instance_vnics(compute, network, compartment_id, inst.id)
        if not instance_vnics:
            raise RuntimeError("No VNIC attachments found.")

        primary_att, primary_vnic = instance_vnics[0]

        # Build IP mapping for LB detection
        ip_to_subnet = get_ip_to_subnet_map_for_instance(network, instance_vnics)

        lb_matches = []
        if args.restore_lb:
            print("[LB] Scanning classic Load Balancers for backend membership (by IP match)...")
            lb_matches = discover_classic_lb_membership(lb, lb_compartment_id, ip_to_subnet)
            if lb_matches:
                print(f"[LB] Found {len(lb_matches)} backend reference(s):")
                for m in lb_matches:
                    print(f"  - {m['lb_name']} | backend_set={m['backend_set_name']} | {m['old_ip']}:{m['port']}")
            else:
                print("[LB] No classic LB backend membership found for this instance.")

        boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, inst.id)
        block_attachments = get_instance_block_volume_attachments(compute, compartment_id, inst.id)

        # Stop (consistent backup)
        stop_instance_if_needed(compute, inst)

        # Backup
        name_prefix = inst.display_name.replace(" ", "_")[:40]
        boot_bkp_id = backup_boot_volume(block, boot_volume_id, name_prefix)
        blk_bkp_map = backup_block_volumes(block, block_attachments, name_prefix)

        # Restore
        new_boot_vol_id = restore_boot_volume(block, compartment_id, dest_ad, boot_bkp_id, name_prefix)
        restored_blk_map = restore_block_volumes(block, compartment_id, dest_ad, blk_bkp_map, name_prefix)

        # New instance
        new_instance_id, new_instance_name = create_new_instance(
            compute, inst, compartment_id, dest_ad, new_boot_vol_id, primary_vnic, ssh_key=ssh_key, new_shape=new_shape
        )

        # Secondary VNICs (to preserve multi-subnet layouts)
        created_vnic_attachment_ids = attach_secondary_vnics(compute, instance_vnics, new_instance_id)

        # Attach restored volumes
        created_volume_attachment_ids = attach_restored_volumes(compute, new_instance_id, block_attachments, restored_blk_map)

        # Fetch NEW instance vnics for LB IP mapping
        new_instance_vnics = get_instance_vnics(compute, network, compartment_id, new_instance_id)
        new_subnet_to_ip = get_subnet_to_primary_ip_for_instance(network, new_instance_vnics)
        new_primary_private_ip = new_subnet_to_ip.get(primary_vnic.subnet_id)

        # Restore LB backend membership
        lb_actions = []
        if lb_matches:
            print("[LB] Restoring backend membership to original LB backend sets...")
            for m in lb_matches:
                # Prefer matching subnet IP, else fallback to new primary IP
                desired_ip = new_subnet_to_ip.get(m["subnet_id"]) or new_primary_private_ip
                if not desired_ip:
                    print("  - LB: could not determine new private IP for this instance; skipping.")
                    continue

                ensure_backend_in_classic_lb(lb_ops, lb, m, desired_ip, disable_old)
                lb_actions.append({
                    "lb_name": m["lb_name"],
                    "lb_id": m["lb_id"],
                    "backend_set": m["backend_set_name"],
                    "old_backend": f"{m['old_ip']}:{m['port']}",
                    "new_backend": f"{desired_ip}:{m['port']}",
                    "disabled_old": disable_old
                })

        print("\n[Bulma] Done ✅")
        print(f"  - New instance: {new_instance_name} ({new_instance_id})")
        print(f"  - New primary private IP: {new_primary_private_ip}")
        print("  - NOTE: Original instance remains STOPPED and unchanged (besides being stopped).")

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

    print("\n" + "#" * 80)
    print("BULMA MIGRATION REPORT")
    print("#" * 80)
    for r in report:
        print(f"\nSource: {r['source_instance']} ({r['source_instance_id']})")
        print(f"  AD: {r['source_ad']} -> {r['dest_ad']}")
        print(f"  New: {r['new_instance']} ({r['new_instance_id']})")
        print(f"  New primary private IP: {r['new_primary_private_ip']}")
        if r["lb_actions"]:
            print("  LB restored:")
            for a in r["lb_actions"]:
                print(f"    - {a['lb_name']} / {a['backend_set']}: {a['old_backend']} -> {a['new_backend']} (old disabled={a['disabled_old']})")
        else:
            print("  LB restored: none / not detected")

    print("\nAll done. 🚀")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCanceled by user.")
        sys.exit(130)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
