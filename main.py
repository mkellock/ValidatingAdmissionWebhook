"""
Admission webhook for validating available IPs in AWS subnets before allowing
resource creation.
"""

import os
import json
import logging
import ipaddress
from flask import Flask, request, jsonify
import boto3
from cachetools import TTLCache, cached
from kubernetes import client, config

# Constants
AWS_RESERVED_IPS = 5
DEFAULT_CACHE_SIZE = 100
DEFAULT_CACHE_TTL = 15
DEFAULT_PORT = 8443

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Cache for subnet info: max 100 entries, TTL 15 seconds
subnet_cache = TTLCache(maxsize=DEFAULT_CACHE_SIZE, ttl=DEFAULT_CACHE_TTL)

# AWS EC2 client
region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
if not region:
    logger.error(
        "AWS region not specified. Set AWS_REGION or AWS_DEFAULT_REGION environment variable."
    )
    raise RuntimeError(
        "AWS region not specified. Set AWS_REGION or AWS_DEFAULT_REGION environment variable."
    )
logger.info(f"Initializing AWS EC2 client in region: {region}")
ec2 = boto3.client("ec2", region_name=region)

# Initialize Kubernetes client
try:
    config.load_incluster_config()
    k8s_client = client.CustomObjectsApi()
    logger.info("Kubernetes client initialized")
except Exception as e:
    logger.error("Failed to initialize Kubernetes client: %s", str(e))
    raise


@cached(subnet_cache)
def describe_subnet(subnet_id):
    """
    Fetch subnet details from AWS and return (total_ips, available_ips).
    Total IPs are calculated as cidr_size - AWS_RESERVED_IPS.
    """
    logger.debug("describe_subnet: querying AWS for SubnetId=%s", subnet_id)
    try:
        resp = ec2.describe_subnets(SubnetIds=[subnet_id])
        if not resp["Subnets"]:
            raise ValueError(f"No subnets found for SubnetId={subnet_id}")
        subnet = resp["Subnets"][0]
        cidr = subnet["CidrBlock"]
        available = subnet["AvailableIpAddressCount"]
        total = ipaddress.ip_network(cidr).num_addresses - AWS_RESERVED_IPS
        logger.debug(
            "describe_subnet: Subnet=%s, CIDR=%s, total_ips=%d, available_ips=%d",
            subnet_id,
            cidr,
            total,
            available,
        )
        return total, available
    except boto3.exceptions.Boto3Error as e:
        logger.error("AWS API error: %s", str(e))
        raise


app = Flask(__name__)

# Check for dry mode
DRY_MODE = os.getenv("DRY_MODE", "false").lower() == "true"
if DRY_MODE:
    logger.warning("Dry mode is enabled. All requests will be allowed.")


@app.route("/validate", methods=["POST"])
def validate():
    """
    AdmissionReview endpoint for validating available IPs in Karpenter
    NodeClaims.
    """
    logger.info("Received AdmissionReview /validate request")
    try:
        req = request.get_json(force=True)
        logger.debug("Raw request JSON: %s", json.dumps(req))
        uid = req["request"]["uid"]
        obj = req["request"]["object"]
    except (KeyError, TypeError, ValueError):
        logger.error("Failed to parse AdmissionReview request", exc_info=True)
        return jsonify({"error": "Invalid AdmissionReview request"}), 400

    # Try different paths to find subnet IDs
    subnet_ids = []

    # Method 1: Direct subnetSelector (old format)
    subnet_selector = obj.get("spec", {}).get("subnetSelector", {})
    subnet_ids_str = subnet_selector.get("aws-ids", "")
    if subnet_ids_str:
        subnet_ids = [s.strip() for s in subnet_ids_str.split(",") if s.strip()]
        logger.info(f"Found subnet IDs in subnetSelector: {subnet_ids}")

    # Method 2: Fetch EC2NodeClass if referenced
    if not subnet_ids:
        node_class_ref = obj.get("spec", {}).get("nodeClassRef", {})
        if node_class_ref:
            node_class_name = node_class_ref.get("name")
            try:
                ec2_node_class = k8s_client.get_namespaced_custom_object(
                    group="karpenter.k8s.aws",
                    version="v1",
                    namespace="kube-system",
                    plural="ec2nodeclasses",
                    name=node_class_name,
                )
                subnet_selector = ec2_node_class.get("spec", {}).get(
                    "subnetSelector", {}
                )
                subnet_ids_str = subnet_selector.get("aws-ids", "")
                if subnet_ids_str:
                    subnet_ids = [
                        s.strip() for s in subnet_ids_str.split(",") if s.strip()
                    ]
                    logger.info(f"Found subnet IDs in EC2NodeClass: {subnet_ids}")
            except client.exceptions.ApiException as e:
                logger.error(
                    "Failed to fetch EC2NodeClass %s: %s", node_class_name, str(e)
                )
                if DRY_MODE:
                    logger.info(
                        "Dry mode: would have rejected request due to missing EC2NodeClass %s",
                        node_class_name,
                    )
                    return admission_response(uid, True)
                return admission_response(
                    uid, False, f"Error fetching EC2NodeClass {node_class_name}"
                )

    # If no subnet IDs found through any method, allow the NodeClaim
    if not subnet_ids:
        logger.info("No subnet IDs found in NodeClaim; defaulting to allow")
        return admission_response(uid, True)

    try:
        throttle_percent = float(os.getenv("THROTTLE_AT_PERCENT", "10"))
    except ValueError:
        logger.error("Invalid THROTTLE_AT_PERCENT value; defaulting to 10")
        throttle_percent = 10.0

    logger.info(
        "Throttle threshold configured at %.1f%% of total IPs", throttle_percent
    )

    # Validate all subnet IDs in the selector
    failed_subnets = []
    for subnet_id in subnet_ids:
        try:
            total, available = describe_subnet(subnet_id)
        except Exception:  # pylint: disable=broad-except
            logger.error("Error fetching subnet info for %s", subnet_id, exc_info=True)
            if DRY_MODE:
                logger.info(
                    "Dry mode: would have rejected request due to error querying subnet %s",
                    subnet_id,
                )
                return admission_response(uid, True)
            return admission_response(uid, False, f"Error querying subnet {subnet_id}")

        threshold = total * (throttle_percent / 100.0)
        logger.info(
            "Subnet %s: available=%d, threshold=%.1f", subnet_id, available, threshold
        )

        if available < threshold:
            percent_free = (available / total) * 100.0 if total else 0
            failed_subnets.append(f"{subnet_id} ({available} IPs, {percent_free:.1f}%)")

    if failed_subnets:
        message = (
            "One or more subnets in NodeClaim have too few available IPs: "
            + ", ".join(failed_subnets)
        )
        logger.warning(message)
        if DRY_MODE:
            logger.info("Dry mode: would have rejected request with UID=%s", uid)
            return admission_response(uid, True)
        return admission_response(uid, False, message)

    logger.info("All subnets in NodeClaim have sufficient IPs")
    return admission_response(uid, True)


def admission_response(uid, allowed, message=None):
    """
    Build the AdmissionReview response.
    """
    logger.debug(
        "Building admission response: uid=%s, allowed=%s, message=%s",
        uid,
        allowed,
        message,
    )
    response = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": allowed,
        },
    }
    if message and not allowed:
        response["response"]["status"] = {"code": 400, "message": message}
    return jsonify(response)


if __name__ == "__main__":
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    logger.info(
        "Starting IP throttle webhook on port %d | log level=%s", port, LOG_LEVEL
    )
    # In production, TLS should be terminated by Kubernetes Ingress/Service
    app.run(host="0.0.0.0", port=port)
