#!/usr/bin/env python3
"""
bulma.py — CapsuleCorp AD-to-AD Compute migration (Backup & Restore) for OCI Cloud Shell

What it does (safe mode):
- Lets you select 1+ instances from a compartment
- Shuts down the selected instance(s)
- Creates backups for:
    - Boot volume
    - All attached block volumes
- Restores volumes into a destination Availability Domain (same region)
- Creates a NEW instance in the destination AD from the restored boot volume
- Re-attaches restored block volumes
- Re-creates VNIC attachments (subnet + NSGs) for primary + secondary VNICs

Important networking notes (because you asked “IP, security group etc”):
- NSGs are preserved (re-attached to new VNICs).
- Subnet is preserved (same subnet IDs by default).
- The *primary private IP* of the original instance cannot be “kept” while the original still exists.
  This script therefore creates new VNIC(s) with NEW private IP(s) by default.
- If the original had a public IP, the new instance can get a NEW ephemeral public IP (optional).
  Reserved public IP reassignment would modify the original assignment, so this script does NOT do that.

Prereqs:
- Run from OCI Cloud Shell (OCI Python SDK is typically available).
- Your Cloud Shell must have permission to read/stop instances, manage block volume backups/restores,
  create instances, and manage VNIC/attachments in the target compartment.

"""

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
        config = {"region": signer.region}
        print(f"[Auth] Using Resource Principals signer (region={config['region']})")
        return config, signer
    except Exception:
        config = oci.config.from_file()
        signer = None
        print(f"[Auth] Using config file (~/.oci/config) (region={config.get('region')})")
        return config, signer


def get_clients(config, signer=None):
    identity = oci.identity.IdentityClient(config, signer=signer)
    compute = oci.core.ComputeClient(config, signer=signer)
    network = oci.core.VirtualNetworkClient(config, signer=signer)
    block = oci.core.BlockstorageClient(config, signer=signer)
    return identity, compute, network, block


# --------------------------
# Helpers
# --------------------------

def prompt(msg, default=None):
    if default is not None:
        val = input(f"{msg} [{default}]: ").strip()
        return val if val else default
    return input(f"{msg}: ").strip()


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
    """
    Polls getter_fn() until it returns an object with .lifecycle_state == desired_state.
    getter_fn must return the SDK model object (not a response).
    """
    start = time.time()
    while True:
        obj = getter_fn()
        state = getattr(obj, "lifecycle_state", None)
        if state == desired_state:
            return obj
        if time.time() - start > max_wait_seconds:
            raise TimeoutError(f"Timed out waiting for {label} to reach state {desired_state}. Last state: {state}")
        print(f"  - waiting for {label}: {state} -> {desired_state} ...")
        time.sleep(interval)


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
    # Filter out terminated
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
    # primary first
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
    # Typically only one
    return bvas[0].boot_volume_id


def get_instance_block_volume_attachments(compute, compartment_id, instance_id):
    vas = list_call_get_all_results(
        compute.list_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data
    return vas


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


def create_new_instance(compute, instance, compartment_id, dest_ad, boot_volume_id, primary_vnic, ssh_key=None):
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_name = f"{instance.display_name}-Bulma-{suffix}"
    print(f"[Compute] Creating new instance in {dest_ad}: {new_name}")

    # Source details (boot volume)
    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(
        boot_volume_id=boot_volume_id
    )

    # Metadata: preserve what we can; optionally ensure SSH key exists for Linux
    metadata = dict(getattr(instance, "metadata", {}) or {})
    if ssh_key:
        metadata["ssh_authorized_keys"] = ssh_key

    # VNIC create details based on original primary VNIC
    assign_public_ip = bool(getattr(primary_vnic, "public_ip", None))
    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=primary_vnic.subnet_id,
        display_name=primary_vnic.display_name or f"{new_name}-vnic0",
        assign_public_ip=assign_public_ip,
        nsg_ids=list(primary_vnic.nsg_ids or [])
        # NOTE: we intentionally do NOT set private_ip to avoid IP collision with original
    )

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=dest_ad,
        compartment_id=compartment_id,
        display_name=new_name,
        shape=instance.shape,
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        metadata=metadata
    )

    resp = compute.launch_instance(details)
    new_instance = resp.data
    wait_for_state(lambda: compute.get_instance(new_instance.id).data, "RUNNING", label="new instance")
    return new_instance.id, new_name


def attach_secondary_vnics(compute, instance_vnics, new_instance_id):
    """
    Re-create additional VNIC attachments (excluding primary).
    Preserves subnet + NSGs. Allocates new private IPs.
    """
    if len(instance_vnics) <= 1:
        return []

    created = []
    for idx, (att, vnic) in enumerate(instance_vnics[1:], start=1):
        print(f"[Network] Creating secondary VNIC attachment #{idx} (subnet preserved, new IP will be allocated)")
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
        # Wait until ATTACHED
        wait_for_state(lambda: compute.get_vnic_attachment(va.id).data, "ATTACHED", label=f"vnic_attachment_{idx}")
        created.append(va.id)
    return created


def attach_restored_volumes(compute, new_instance_id, original_attachments, restored_map):
    """
    Attach restored block volumes to the new instance.
    Tries to preserve attachment type and basic flags.
    """
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
            # Default to paravirtualized
            details = oci.core.models.AttachParavirtualizedVolumeDetails(
                instance_id=new_instance_id,
                volume_id=new_vol_id,
                is_read_only=is_read_only,
                display_name=getattr(att, "display_name", None)
            )
            # Some tenancies allow specifying device; if it fails, OCI will assign automatically.
            device = getattr(att, "device", None)
            if device:
                details.device = device

        va = compute.attach_volume(details).data
        # Wait until ATTACHED
        wait_for_state(lambda: compute.get_volume_attachment(va.id).data, "ATTACHED", label="volume_attachment")
        attached.append(va.id)

    return attached


def fetch_new_primary_vnic(network, compute, compartment_id, new_instance_id):
    vnic_atts = list_call_get_all_results(
        compute.list_vnic_attachments,
        compartment_id=compartment_id,
        instance_id=new_instance_id
    ).data
    primary = None
    for att in vnic_atts:
        if getattr(att, "is_primary", False):
            primary = att
            break
    if not primary and vnic_atts:
        primary = vnic_atts[0]
    if not primary:
        return None
    vnic = network.get_vnic(primary.vnic_id).data
    return vnic


# --------------------------
# Main
# --------------------------

def main():
    config, signer = get_signer_and_config()
    identity, compute, network, block = get_clients(config, signer)

    compartment_id = prompt("Enter COMPARTMENT OCID (where the source instances exist)")
    if not compartment_id.startswith("ocid1.compartment"):
        print("Compartment OCID does not look right. Exiting.")
        sys.exit(1)

    # Availability domains
    ads = identity.list_availability_domains(config["tenancy"], compartment_id).data \
        if "tenancy" in config else identity.list_availability_domains(prompt("Enter TENANCY OCID"), compartment_id).data
    # Some config objects include tenancy in ~/.oci/config; Resource principals config won't.
    if not ads:
        print("No availability domains found. Exiting.")
        sys.exit(1)

    dest_ad_obj = choose_from_list(
        "Select destination Availability Domain",
        ads,
        lambda ad: ad.name
    )
    dest_ad = dest_ad_obj.name

    # Optional SSH key
    ssh_key = None
    if yn("Provide/override SSH public key in metadata for NEW instances? (recommended for Linux)", default="y"):
        ssh_key = prompt("Paste SSH public key (single line)", default="")

    # List instances
    instances = list_instances_in_compartment(compute, compartment_id)
    if not instances:
        print("No instances found in that compartment.")
        sys.exit(0)

    print("\nInstances")
    print("---------")
    for idx, inst in enumerate(instances, 1):
        print(f"{idx:>2}. {inst.display_name} | {inst.lifecycle_state} | {inst.availability_domain} | {inst.shape} | {inst.id}")

    chosen = choose_multi_indices(len(instances))
    selected = [instances[i] for i in chosen]

    print("\nPlanned actions (per instance)")
    print("- Stop instance (shutdown)")
    print("- Backup boot + block volumes")
    print("- Restore volumes into destination AD")
    print("- Create new instance from restored boot volume")
    print("- Re-create VNICs with same subnet + NSGs (new private IPs)")
    print("- Attach restored block volumes")
    if not yn("Continue?", default="y"):
        print("Canceled.")
        sys.exit(0)

    report = []

    for inst in selected:
        print("\n" + "=" * 80)
        print(f"[Bulma] Migrating: {inst.display_name} ({inst.id})")
        print("=" * 80)

        # Refresh instance object
        inst = compute.get_instance(inst.id).data

        # Collect network
        instance_vnics = get_instance_vnics(compute, network, compartment_id, inst.id)
        if not instance_vnics:
            raise RuntimeError("No VNIC attachments found.")
        primary_att, primary_vnic = instance_vnics[0]

        # Collect storage
        boot_volume_id = get_instance_boot_volume_id(compute, compartment_id, inst.id)
        block_attachments = get_instance_block_volume_attachments(compute, compartment_id, inst.id)

        # Stop
        stop_instance_if_needed(compute, inst)

        # Backup
        name_prefix = inst.display_name.replace(" ", "_")[:40]
        boot_bkp_id = backup_boot_volume(block, boot_volume_id, name_prefix)
        blk_bkp_map = backup_block_volumes(block, block_attachments, name_prefix)

        # Restore
        new_boot_vol_id = restore_boot_volume(block, compartment_id, dest_ad, boot_bkp_id, name_prefix)
        restored_blk_map = restore_block_volumes(block, compartment_id, dest_ad, blk_bkp_map, name_prefix)

        # Create new instance
        new_instance_id, new_instance_name = create_new_instance(
            compute, inst, compartment_id, dest_ad, new_boot_vol_id, primary_vnic, ssh_key=ssh_key
        )

        # Secondary VNICs
        vnic_attachment_ids = attach_secondary_vnics(compute, instance_vnics, new_instance_id)

        # Attach volumes
        volume_attachment_ids = attach_restored_volumes(compute, new_instance_id, block_attachments, restored_blk_map)

        # Fetch new primary VNIC info for report
        new_primary_vnic = fetch_new_primary_vnic(network, compute, compartment_id, new_instance_id)
        new_private_ip = getattr(new_primary_vnic, "private_ip", None) if new_primary_vnic else None
        new_public_ip = getattr(new_primary_vnic, "public_ip", None) if new_primary_vnic else None

        print("\n[Bulma] Done ✅")
        print(f"  - New instance: {new_instance_name} ({new_instance_id})")
        print(f"  - New primary private IP: {new_private_ip}")
        print(f"  - New public IP (if any): {new_public_ip}")
        print("  - NOTE: Original instance remains STOPPED and unchanged (besides being stopped).")

        report.append({
            "source_instance": inst.display_name,
            "source_instance_id": inst.id,
            "source_ad": inst.availability_domain,
            "dest_ad": dest_ad,
            "new_instance": new_instance_name,
            "new_instance_id": new_instance_id,
            "new_private_ip": new_private_ip,
            "new_public_ip": new_public_ip,
            "boot_backup_id": boot_bkp_id,
            "restored_boot_volume_id": new_boot_vol_id,
            "restored_block_volumes": restored_blk_map,
            "created_vnic_attachments": vnic_attachment_ids,
            "created_volume_attachments": volume_attachment_ids
        })

    print("\n" + "#" * 80)
    print("BULMA MIGRATION REPORT")
    print("#" * 80)
    for r in report:
        print(f"\nSource: {r['source_instance']} ({r['source_instance_id']})")
        print(f"  Source AD: {r['source_ad']}  ->  Destination AD: {r['dest_ad']}")
        print(f"  New: {r['new_instance']} ({r['new_instance_id']})")
        print(f"  New IPs: private={r['new_private_ip']} public={r['new_public_ip']}")
        print(f"  Boot backup: {r['boot_backup_id']}")
        print(f"  Restored boot volume: {r['restored_boot_volume_id']}")
        print(f"  Restored block volumes count: {len(r['restored_block_volumes'])}")

    print("\nAll done. 🚀")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCanceled by user.")
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
