"""
Deploy the Semantic Access Gate registry and the gated consumer to StudioNet.

The consumer is deployed second because it pins the registry address at construction.
Both addresses, the deployment transactions and the resolved ABI are written to
artifacts/deployment.json, which the live test suite reads.

Usage:
    python scripts/deploy.py [--gate-id GATE_ID]
"""

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time

from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_SOURCE = ROOT / "contracts" / "semantic_access_gate.py"
CONSUMER_SOURCE = ROOT / "tests" / "fixtures" / "gated_consumer.py"
ARTIFACT = ROOT / "deployments" / "studionet.json"

RUNNER = "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6"

# The deploying key is read from the environment and is deliberately not in the source.
# A key committed to a repository is a key that has been published, whichever network it
# happens to fund.
DEPLOYER_KEY_VAR = "GENLAYER_DEPLOYER_KEY"


def deploy(client, source: pathlib.Path, args: list, label: str) -> str:
    print(f"deploying {label} from {source.name} ...", flush=True)
    code = source.read_text()
    tx_hash = client.deploy_contract(code=code, args=args)
    print(f"  tx {tx_hash}", flush=True)
    receipt = client.wait_for_transaction_receipt(tx_hash, retries=80, interval=3000)
    status = receipt.get("status_name") or receipt.get("status")
    address = (
        receipt.get("data", {}).get("contract_address")
        or receipt.get("contract_address")
        or receipt.get("to_address")
    )
    print(f"  status {status}  address {address}", flush=True)
    if not address:
        raise SystemExit(f"{label} deployment returned no contract address: {receipt}")
    return address, tx_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-id", default="licensed-medical-pro")
    parser.add_argument(
        "--registry",
        default=None,
        help="reuse an already deployed registry at this address and deploy only the consumer",
    )
    opts = parser.parse_args()

    key = os.environ.get(DEPLOYER_KEY_VAR, "").strip()
    if not key:
        raise SystemExit(
            f"Set {DEPLOYER_KEY_VAR} to the deploying account's private key, for example:\n"
            f"  export {DEPLOYER_KEY_VAR}=0x...\n"
            "Fund the account on StudioNet with the sim_fundAccount RPC method if its "
            "balance is zero."
        )

    account = create_account(key)
    client = create_client(chain=studionet, account=account)
    balance = client.get_balance(account.address)
    print(f"deployer {account.address} balance {balance}", flush=True)
    if balance == 0:
        raise SystemExit("deployer has no balance; fund it before deploying")

    if opts.registry:
        registry_address = opts.registry
        existing = json.loads(ARTIFACT.read_text()) if ARTIFACT.exists() else {}
        registry_tx = existing.get("registryDeploymentTransaction", "")
        print(f"reusing registry {registry_address}", flush=True)
        probe = client.read_contract(
            address=registry_address, function_name="get_registry_stats", args=[]
        )
        print(f"  registry responds: {probe}", flush=True)
    else:
        registry_address, registry_tx = deploy(
            client, REGISTRY_SOURCE, [], "SemanticAccessGate"
        )
    time.sleep(2)
    consumer_address, consumer_tx = deploy(
        client,
        CONSUMER_SOURCE,
        [registry_address, opts.gate_id, account.address],
        "GatedConsumer",
    )

    schema = client.get_contract_schema(registry_address)
    methods = sorted((schema.get("methods") or {}).keys())

    payload = {
        "network": "studionet",
        "rpc": "https://studio.genlayer.com/api",
        "chainId": "61999",
        "runner": RUNNER,
        "deployerAddress": account.address,
        "registryContractName": "SemanticAccessGate",
        "registryAddress": registry_address,
        "registryDeploymentTransaction": registry_tx,
        "consumerContractName": "GatedConsumer",
        "consumerAddress": consumer_address,
        "consumerDeploymentTransaction": consumer_tx,
        "consumerBoundGateId": opts.gate_id,
        "schemaMethods": methods,
        # So a reviewer can confirm the deployed contracts were built from the sources in
        # this repository rather than from something else.
        "registrySourceSha256": hashlib.sha256(REGISTRY_SOURCE.read_bytes()).hexdigest(),
        "consumerSourceSha256": hashlib.sha256(CONSUMER_SOURCE.read_bytes()).hexdigest(),
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {ARTIFACT}", flush=True)
    print(f"registry: {registry_address}", flush=True)
    print(f"consumer: {consumer_address}", flush=True)
    print(f"methods:  {len(methods)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
