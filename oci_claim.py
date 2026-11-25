#!/usr/bin/env python3
"""Claim an Oracle Cloud Always Free Ampere A1 instance (for Minecraft).

Retries until capacity is available, then saves details, opens the Minecraft
ports in the subnet security list, and notifies you. All settings come from
environment variables (see the workflow file).

Exit behaviour (also written to $GITHUB_OUTPUT as `status`):
  created  - instance launched
  exists   - an A1 instance already exists, nothing to do
  timeout  - ran out of time budget; the next scheduled run continues
  fatal    - non-retryable problem (bad config, auth, quota); exit code 1
"""
import base64
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

import oci

SHAPE = "VM.Standard.A1.Flex"
START = time.time()
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SECONDS") or 20400)  # 5h40m
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS") or 60)
MC_PORTS = [("tcp", 25565), ("udp", 19132)]  # Java, Bedrock (Geyser)

CLOUD_INIT = """#cloud-config
package_update: true
packages:
  - openjdk-21-jre-headless
  - screen
  - unzip
runcmd:
  - iptables -I INPUT 1 -p tcp --dport 25565 -j ACCEPT
  - iptables -I INPUT 1 -p udp --dport 19132 -j ACCEPT
  - netfilter-persistent save
  - mkdir -p /home/ubuntu/minecraft
  - chown ubuntu:ubuntu /home/ubuntu/minecraft
"""


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def getenv(name, default=""):
    return (os.environ.get(name) or default).strip()


def set_status(status):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"status={status}\n")


def notify(text):
    """Best-effort Telegram / Discord notification."""
    token, chat = getenv("TELEGRAM_BOT_TOKEN"), getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15
            )
        except Exception as e:  # noqa: BLE001
            log(f"Telegram notify failed: {e}")
    hook = getenv("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            req = urllib.request.Request(
                hook,
                data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "oci-claimer"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:  # noqa: BLE001
            log(f"Discord notify failed: {e}")


def die(msg):
    log(f"FATAL: {msg}")
    notify(f"❌ OCI claimer stopped: {msg}")
    set_status("fatal")
    sys.exit(1)


def wait(seconds):
    if time.time() - START + seconds >= MAX_RUNTIME:
        log("Reached max runtime; the next scheduled run will continue.")
        set_status("timeout")
        sys.exit(0)
    time.sleep(seconds)


FREE_OCPUS, FREE_MEMORY_GB = 2.0, 12.0   # Oracle Always Free A1 allowance (checked Nov 2026)


def check_free_allowance():
    """Refuse to launch above the free allowance unless explicitly overridden.

    On Pay As You Go, Oracle will happily create a bigger A1 and bill the extra
    hours, so this guard protects you from accidental charges.
    """
    ocpus = float(getenv("OCI_OCPUS", "2"))
    mem = float(getenv("OCI_MEMORY_GB", "12"))
    boot = int(getenv("OCI_BOOT_GB", "200"))
    if boot > 200 and getenv("ALLOW_PAID_SIZE") != "true":
        die(f"OCI_BOOT_GB={boot} exceeds the 200 GB Always Free storage limit and would be billed.")
    if (ocpus > FREE_OCPUS or mem > FREE_MEMORY_GB) and getenv("ALLOW_PAID_SIZE") != "true":
        die(
            f"Requested {ocpus} OCPU / {mem} GB exceeds the Always Free allowance "
            f"({FREE_OCPUS} OCPU / {FREE_MEMORY_GB} GB) and would be billed on a paid "
            "account. Lower OCI_OCPUS/OCI_MEMORY_GB, or set ALLOW_PAID_SIZE=true if you accept the cost."
        )


# ---------------------------------------------------------------- OCI setup
def load_config():
    key = getenv("OCI_PRIVATE_KEY")
    # Tolerate keys pasted with literal "\n" instead of real newlines.
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    config = {
        "user": getenv("OCI_USER_ID"),
        "fingerprint": getenv("OCI_KEY_FINGERPRINT"),
        "tenancy": getenv("OCI_TENANCY_ID"),
        "region": getenv("OCI_REGION"),
        "key_content": key,
    }
    try:
        oci.config.validate_config(config)
    except Exception as e:  # noqa: BLE001
        die(f"Invalid OCI config (check secrets): {e}")
    return config


def get_availability_domains(identity, tenancy):
    fixed = getenv("OCI_AVAILABILITY_DOMAIN")
    if fixed:
        return [fixed]
    return [ad.name for ad in identity.list_availability_domains(tenancy).data]


def find_existing_a1(compute, compartment):
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment
    ).data
    return [
        i for i in instances
        if i.shape.startswith("VM.Standard.A1")
        and i.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


def find_image(compute, tenancy):
    explicit = getenv("OCI_IMAGE_ID")
    if explicit:
        return explicit
    version = getenv("OCI_UBUNTU_VERSION", "24.04")
    images = compute.list_images(
        tenancy,
        operating_system="Canonical Ubuntu",
        operating_system_version=version,
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
        limit=20,
    ).data
    images = [i for i in images if "minimal" not in i.display_name.lower()]
    if not images:
        die(f"No Ubuntu {version} ARM image found. Set OCI_IMAGE_ID manually.")
    log(f"Auto-selected image: {images[0].display_name}")
    return images[0].id


def build_details(ad, compartment, subnet_id, image_id, ssh_key):
    user_data = base64.b64encode(CLOUD_INIT.encode()).decode()
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=compartment,
        shape=SHAPE,
        display_name=getenv("OCI_INSTANCE_NAME", "minecraft-server"),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=int(getenv("OCI_BOOT_GB", "200")),
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            assign_public_ip=True, subnet_id=subnet_id
        ),
        metadata={"ssh_authorized_keys": ssh_key, "user_data": user_data},
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(getenv("OCI_OCPUS", "2")),
            memory_in_gbs=float(getenv("OCI_MEMORY_GB", "12")),
        ),
    )


def classify(e):
    """Map an OCI ServiceError to retry behaviour."""
    msg = (e.message or "").lower()
    if e.status == 429:
        return "ratelimit"
    if "out of host capacity" in msg or "out of capacity" in msg or e.status >= 500:
        return "capacity"
    if e.status == 409:
        return "retry"
    return "fatal"


# ---------------------------------------------------------------- post-launch
def has_rule(rules, number, port):
    for r in rules:
        if r.protocol == "all" and r.source == "0.0.0.0/0":
            return True
        if r.protocol != number or r.source != "0.0.0.0/0":
            continue
        opts = r.tcp_options if number == "6" else r.udp_options
        if opts is None or opts.destination_port_range is None:
            return True
        rng = opts.destination_port_range
        if rng.min <= port <= rng.max:
            return True
    return False


def open_ports(network, subnet_id):
    """Add Minecraft ingress rules to the subnet's first security list."""
    try:
        subnet = network.get_subnet(subnet_id).data
        sl_id = subnet.security_list_ids[0]
        rules = list(network.get_security_list(sl_id).data.ingress_security_rules)
        changed = False
        for proto, port in MC_PORTS:
            number = "6" if proto == "tcp" else "17"
            if has_rule(rules, number, port):
                continue
            opts = oci.core.models.PortRange(min=port, max=port)
            rules.append(
                oci.core.models.IngressSecurityRule(
                    protocol=number,
                    source="0.0.0.0/0",
                    source_type="CIDR_BLOCK",
                    is_stateless=False,
                    description=f"Minecraft {proto}/{port}",
                    tcp_options=oci.core.models.TcpOptions(destination_port_range=opts)
                    if proto == "tcp" else None,
                    udp_options=oci.core.models.UdpOptions(destination_port_range=opts)
                    if proto == "udp" else None,
                )
            )
            changed = True
        if changed:
            network.update_security_list(
                sl_id,
                oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules),
            )
            log("✓ Opened 25565/tcp and 19132/udp in the security list")
        else:
            log("✓ Security list already allows Minecraft ports")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"⚠ Could not update security list (open ports manually): {e}")
        return False


def finish(compute, network, instance, ad, compartment, subnet_id, image_id, config):
    public_ip, boot_volume_id = "pending (check console)", "check-oracle-console"

    try:
        oci.wait_until(
            compute,
            compute.get_instance(instance.id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=900,
        )
    except Exception as e:  # noqa: BLE001
        log(f"⚠ Wait for RUNNING failed: {e}")

    for _ in range(10):
        try:
            att = compute.list_vnic_attachments(compartment, instance_id=instance.id).data
            if att:
                ip = network.get_vnic(att[0].vnic_id).data.public_ip
                if ip:
                    public_ip = ip
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(10)

    try:
        boot = compute.list_boot_volume_attachments(
            ad, compartment, instance_id=instance.id
        ).data
        if boot:
            boot_volume_id = boot[0].boot_volume_id
    except Exception as e:  # noqa: BLE001
        log(f"⚠ Could not read boot volume: {e}")

    ports_ok = open_ports(network, subnet_id)

    details = f"""INSTANCE_ID={instance.id}
PUBLIC_IP={public_ip}
BOOT_VOLUME_ID={boot_volume_id}
COMPARTMENT_ID={compartment}
SUBNET_ID={subnet_id}
IMAGE_ID={image_id}
AVAILABILITY_DOMAIN={ad}
REGION={config['region']}
SHAPE={instance.shape}

--- SSH ---
ssh -i fixed-key.pem ubuntu@{public_ip}

--- MINECRAFT ---
Java:    {public_ip}:25565
Bedrock: {public_ip}:19132 (needs Geyser)
Security list opened automatically: {ports_ok}
Folder prepared by cloud-init: /home/ubuntu/minecraft
(cloud-init installs Java 21; give it ~3-5 minutes after first boot)

--- RECOVERY (Cloud Shell) ---
oci compute instance launch \\
  --availability-domain {ad} \\
  --compartment-id {compartment} \\
  --shape {SHAPE} \\
  --shape-config '{{"ocpus":{getenv("OCI_OCPUS", "2")},"memoryInGBs":{getenv("OCI_MEMORY_GB", "12")}}}' \\
  --source-details '{{"sourceType":"bootVolume","bootVolumeId":"{boot_volume_id}"}}' \\
  --subnet-id {subnet_id} \\
  --assign-public-ip true \\
  --display-name minecraft-server \\
  --ssh-authorized-keys-file ~/ssh-public-key.pub
"""
    with open("instance-details.txt", "w") as f:
        f.write(details)

    notify(
        "🎉 Oracle A1 instance created!\n"
        f"IP: {public_ip}\nInstance: {instance.id}\n"
        f"SSH: ssh -i fixed-key.pem ubuntu@{public_ip}\n"
        f"Minecraft: {public_ip}:25565"
    )
    log("🎉 Instance created. Instance OCID: " + instance.id)
    log("Details saved to instance-details.txt (IP intentionally not printed).")
    set_status("created")


# ---------------------------------------------------------------- main
def main():
    check_free_allowance()
    config = load_config()
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    identity = oci.identity.IdentityClient(config)

    compartment = getenv("OCI_COMPARTMENT_ID") or config["tenancy"]
    subnet_id = getenv("OCI_SUBNET_ID")
    ssh_key = getenv("SSH_PUBLIC_KEY")
    if not subnet_id:
        die("OCI_SUBNET_ID secret is missing")
    if not ssh_key.startswith("ssh-"):
        die("SSH_PUBLIC_KEY secret is missing or not a valid public key")

    try:
        existing = find_existing_a1(compute, compartment)
        if existing:
            names = ", ".join(f"{i.display_name} ({i.lifecycle_state})" for i in existing)
            log(f"✓ A1 instance already exists: {names}. Nothing to do.")
            notify(f"ℹ️ OCI claimer: A1 instance already exists ({names}). Stopping.")
            set_status("exists")
            return
        ads = get_availability_domains(identity, config["tenancy"])
        image_id = find_image(compute, config["tenancy"])
    except oci.exceptions.ServiceError as e:
        die(f"Setup failed: {e.status} {e.code} - {e.message}")

    log(f"Availability domains: {ads}")
    log(
        f"Target: {getenv('OCI_OCPUS', '2')} OCPU / {getenv('OCI_MEMORY_GB', '12')} GB / "
        f"{getenv('OCI_BOOT_GB', '200')} GB boot, retry ~{RETRY_SECONDS}s"
    )

    attempt = ratelimits = unknown = 0
    while True:
        attempt += 1
        ad = ads[(attempt - 1) % len(ads)]
        try:
            resp = compute.launch_instance(
                build_details(ad, compartment, subnet_id, image_id, ssh_key)
            )
            finish(compute, network, resp.data, ad, compartment, subnet_id, image_id, config)
            return
        except oci.exceptions.ServiceError as e:
            unknown = 0
            kind = classify(e)
            if kind == "fatal":
                die(f"{e.status} {e.code}: {e.message}")
            if kind == "ratelimit":
                ratelimits += 1
                delay = min(900, 120 * 2 ** (ratelimits - 1)) + random.uniform(0, 30)
                log(f"#{attempt} 429 rate limited. Backing off {delay:.0f}s")
            else:
                ratelimits = 0
                delay = RETRY_SECONDS + random.uniform(0, RETRY_SECONDS * 0.5)
                log(f"#{attempt} [{ad}] {kind}: {e.status} - retry in {delay:.0f}s")
        except Exception as e:  # noqa: BLE001  (network hiccups etc.)
            unknown += 1
            if unknown >= 10:
                die(f"10 consecutive unexpected errors, last: {e}")
            delay = 30 + random.uniform(0, 15)
            log(f"#{attempt} transient error ({type(e).__name__}): {e}. Retry in {delay:.0f}s")
        wait(delay)


if __name__ == "__main__":
    main()
