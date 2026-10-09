#!/usr/bin/env python3
"""Create the AuntieJoy Lighthouse instance in ap-singapore.

This talks to the Tencent Cloud API directly, so it needs a CAM key pair:

    export TENCENTCLOUD_SECRET_ID=...
    export TENCENTCLOUD_SECRET_KEY=...

Create that key in the console: Access Management (CAM) > API Keys. A SUB-USER key with
QcloudLighthouseFullAccess is the right choice (not the root key), and it can be deleted
afterwards. If you also want this script to write DNS, add DNSPod permissions.

What it does, in order:
  1. Finds the Ubuntu blueprint and the Linux Starter bundle (or uses the ids you pin).
  2. Imports your SSH public key, so no console step is needed to attach it.
  3. Creates ONE Lighthouse instance with that key as the ONLY login method (no password).
  4. Waits for RUNNING and prints the public IP.
  5. Opens exactly the ports the deployment needs: 22, 80, 443.

It does NOT buy a domain and does NOT touch DNS. Those are separate, deliberate acts.

Start with --dry-run: it uses the API's own DryRun pre-check, so it validates the request
(inventory, parameters, quota) WITHOUT creating or charging anything.

Verified against the Lighthouse v20200324 API models: LoginConfiguration takes
{AutoGeneratePassword, Password, KeyIds}; InstanceChargePrepaid {Period, RenewFlag} is
mandatory; FirewallRule {Protocol, Port, CidrBlock, Action, FirewallRuleDescription}.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.lighthouse.v20200324 import lighthouse_client, models

REGION = "ap-singapore"        # the owner's chosen region; matches the app's voice endpoints
DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/dbsjoy_lighthouse.pub")
KEY_NAME = "dbsjoy-lighthouse"
INSTANCE_NAME = "DBSJoy-demo"
FIREWALL_PORTS = ["22", "80", "443"]   # 8078 is deliberately NOT opened: Nginx fronts the app


def _client(secret_id: str, secret_key: str) -> lighthouse_client.LighthouseClient:
    cred = credential.Credential(secret_id, secret_key)
    http = HttpProfile(endpoint="lighthouse.tencentcloudapi.com")
    return lighthouse_client.LighthouseClient(cred, REGION, ClientProfile(httpProfile=http))


def find_blueprint(client, want: str = "ubuntu") -> tuple[str, str]:
    """The Ubuntu 22.04 blueprint (image) in this region, preferring 22.04."""
    req = models.DescribeBlueprintsRequest()
    req.Limit = 100
    resp = client.DescribeBlueprints(req)
    found = [(b.BlueprintId, b.BlueprintName or "")
             for b in (resp.BlueprintSet or []) if want in (b.BlueprintName or "").lower()]
    if not found:
        raise SystemExit(f"no blueprint matching {want!r} in {REGION}")
    preferred = [f for f in found if "22.04" in f[1].lower()] or found
    return preferred[0]


def find_bundle(client, target_price: float = 4.20) -> tuple[str, str]:
    """The Linux Starter bundle nearest USD 4.20/month.

    Bundle ids and prices change over time, so this DISCOVERS rather than hard-codes. Pin one
    with --bundle-id once you have seen the list.
    """
    req = models.DescribeBundlesRequest()
    req.Limit = 100
    resp = client.DescribeBundles(req)
    bundles = list(resp.BundleSet or [])
    if not bundles:
        raise SystemExit("no bundles returned for this region")
    priced = []
    for b in bundles:
        price = getattr(b, "Price", None)
        if isinstance(price, (int, float)) and price > 0:
            priced.append((b.BundleId, float(price)))
    if not priced:
        return bundles[0].BundleId, f"(price not returned; first of {len(bundles)} bundles)"
    priced.sort(key=lambda x: x[1])
    at_or_above = [p for p in priced if p[1] >= target_price * 0.8]
    pick = (at_or_above or priced)[0]
    return pick[0], f"USD {pick[1]:.2f}/month"


def import_key(client, ssh_pubkey: str) -> str:
    """Import the public key and return its KeyId (idempotent: reuses an existing name)."""
    existing = models.DescribeKeyPairsRequest()
    existing.Limit = 100
    resp = client.DescribeKeyPairs(existing)
    for kp in resp.KeyPairSet or []:
        if kp.KeyName == KEY_NAME:
            print(f"key pair      : reusing existing '{KEY_NAME}' ({kp.KeyId})")
            return kp.KeyId
    req = models.ImportKeyPairRequest()
    req.KeyName = KEY_NAME
    req.PublicKey = ssh_pubkey
    out = client.ImportKeyPair(req)
    print(f"key pair      : imported as '{KEY_NAME}' ({out.KeyId})")
    return out.KeyId


def create(client, blueprint_id: str, bundle_id: str, key_id: str,
           months: int, dry_run: bool) -> str:
    """Create the instance. Returns the instance id ('' when dry-run)."""
    req = models.CreateInstancesRequest()
    req.BundleId = bundle_id
    req.BlueprintId = blueprint_id
    req.InstanceName = INSTANCE_NAME
    req.InstanceCount = 1
    # Prepaid is the only mode Lighthouse offers, and the period is mandatory.
    prepaid = models.InstanceChargePrepaid()
    prepaid.Period = months
    prepaid.RenewFlag = "NOTIFY_AND_MANUAL_RENEW"   # never auto-charge the card
    req.InstanceChargePrepaid = prepaid
    # SSH key only. Password login is left unset entirely rather than auto-generated.
    login = models.LoginConfiguration()
    login.KeyIds = [key_id]
    req.LoginConfiguration = login
    # ClientToken makes a retried call idempotent instead of buying a second instance.
    req.ClientToken = str(uuid.uuid4())
    req.DryRun = dry_run

    resp = client.CreateInstances(req)
    if dry_run:
        return ""
    return resp.InstanceIdSet[0]


def open_firewall(client, instance_id: str) -> None:
    """Open exactly the ports the deployment needs, from anywhere."""
    rules = []
    for port in FIREWALL_PORTS:
        rule = models.FirewallRule()
        rule.Protocol = "TCP"
        rule.Port = port
        rule.CidrBlock = "0.0.0.0/0"
        rule.Action = "ACCEPT"
        rule.FirewallRuleDescription = f"AuntieJoy: {port}"
        rules.append(rule)
    req = models.CreateFirewallRulesRequest()
    req.InstanceId = instance_id
    req.FirewallRules = rules
    client.CreateFirewallRules(req)
    print(f"firewall      : opened {', '.join(FIREWALL_PORTS)} (8078 stays closed)")


def wait_running(client, instance_id: str) -> dict | None:
    for _ in range(60):
        req = models.DescribeInstancesRequest()
        req.InstanceIds = [instance_id]
        resp = client.DescribeInstances(req)
        if resp.InstanceSet:
            inst = resp.InstanceSet[0]
            if inst.InstanceState == "RUNNING":
                return {"instance_id": instance_id, "state": inst.InstanceState,
                        "public_ip": inst.PublicAddresses[0] if inst.PublicAddresses else ""}
        time.sleep(10)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="use the API's DryRun pre-check: validates, creates nothing")
    ap.add_argument("--bundle-id", default=os.getenv("LIGHTHOUSE_BUNDLE_ID", ""))
    ap.add_argument("--blueprint-id", default=os.getenv("LIGHTHOUSE_BLUEPRINT_ID", ""))
    ap.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--no-firewall", action="store_true")
    args = ap.parse_args()

    secret_id = os.getenv("TENCENTCLOUD_SECRET_ID", "")
    secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY", "")
    if not (secret_id and secret_key):
        print("ERROR: set TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY first.\n"
              "       Console: Access Management (CAM) > API Keys.", file=sys.stderr)
        return 2
    if not os.path.exists(args.ssh_key):
        print(f"ERROR: SSH public key not found at {args.ssh_key}\n"
              f"       Generate one: ssh-keygen -t ed25519 -f ~/.ssh/dbsjoy_lighthouse",
              file=sys.stderr)
        return 2

    ssh_pubkey = open(args.ssh_key, encoding="utf-8").read().strip()
    client = _client(secret_id, secret_key)

    print(f"region        : {REGION}")
    print(f"ssh key       : {args.ssh_key}")
    print(f"period        : {args.months} month(s), manual renewal")

    try:
        blueprint_id, blueprint_name = ((args.blueprint_id, "(pinned)")
                                        if args.blueprint_id else find_blueprint(client))
        print(f"blueprint     : {blueprint_id}  {blueprint_name}")

        bundle_id, bundle_desc = ((args.bundle_id, "(pinned)")
                                  if args.bundle_id else find_bundle(client))
        print(f"bundle        : {bundle_id}  {bundle_desc}")

        key_id = import_key(client, ssh_pubkey)

        print(f"instance name : {INSTANCE_NAME}")
        print(f"firewall      : {', '.join(FIREWALL_PORTS)} (8078 stays CLOSED)")

        if args.dry_run:
            print("\n--dry-run: asking the API to validate (nothing is created or charged) ...")
            create(client, blueprint_id, bundle_id, key_id, args.months, dry_run=True)
            print("API accepted the request. Re-run without --dry-run to create it.")
            return 0

        print("\ncreating instance ...")
        instance_id = create(client, blueprint_id, bundle_id, key_id, args.months, dry_run=False)
        print(f"instance id   : {instance_id}")

        if not args.no_firewall:
            open_firewall(client, instance_id)

        info = wait_running(client, instance_id)
        if info:
            print(f"state         : {info['state']}")
            print(f"public ip     : {info['public_ip']}")
            print(f"\nNext: ./deploy/deploy.sh {info['public_ip']}")
        else:
            print("still not RUNNING after the wait window; check the console.")
        return 0

    except TencentCloudSDKException as exc:
        print(f"\nTencent Cloud API error: {exc.code}: {exc.message}", file=sys.stderr)
        if "AuthFailure" in (exc.code or ""):
            print("  -> the key is wrong, expired, or lacks Lighthouse permissions.",
                  file=sys.stderr)
        if "ResourcesSoldOut" in (exc.code or ""):
            print("  -> that bundle has no stock in this zone; try another zone or bundle.",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
