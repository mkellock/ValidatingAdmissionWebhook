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

# Constants
AWS_RESERVED_IPS = 5
DEFAULT_CACHE_SIZE = 100
DEFAULT_CACHE_TTL = 15
DEFAULT_PORT = 8443

# Configure logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s %(levelname)s %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Cache for subnet info: max 100 entries, TTL 15 seconds
subnet_cache = TTLCache(
    maxsize=DEFAULT_CACHE_SIZE,
    ttl=DEFAULT_CACHE_TTL
)

# AWS EC2 client
logger.info("Initializing AWS EC2 client")
ec2 = boto3.client('ec2')


@cached(subnet_cache)
def describe_subnet(subnet_id):
    """
    Fetch subnet details from AWS and return (total_ips, available_ips).
    Total IPs are calculated as cidr_size - AWS_RESERVED_IPS.
    """
    logger.debug(
        "describe_subnet: querying AWS for SubnetId=%s",
        subnet_id
    )
    resp = ec2.describe_subnets(SubnetIds=[subnet_id])
    subnet = resp['Subnets'][0]
    cidr = subnet['CidrBlock']
    available = subnet['AvailableIpAddressCount']
    total = ipaddress.ip_network(cidr).num_addresses - AWS_RESERVED_IPS
    logger.debug(
        "describe_subnet: Subnet=%s, CIDR=%s, total_ips=%d, available_ips=%d",
        subnet_id, cidr, total, available
    )
    return total, available


app = Flask(__name__)


@app.route('/validate', methods=['POST'])
def validate():
    """
    AdmissionReview endpoint for validating available IPs in Karpenter
    NodeClaims.
    """
    logger.info("Received AdmissionReview /validate request")
    try:
        req = request.get_json(force=True)
        logger.debug(
            "Raw request JSON: %s",
            json.dumps(req)
        )
        uid = req['request']['uid']
        obj = req['request']['object']
    except (KeyError, TypeError, ValueError):
        logger.error(
            "Failed to parse AdmissionReview request",
            exc_info=True
        )
        return jsonify({'error': 'Invalid AdmissionReview request'}), 400

    # Extract subnet IDs from Karpenter NodeClaim spec
    subnet_selector = obj.get('spec', {}).get('subnetSelector', {})
    subnet_ids_str = subnet_selector.get('aws-ids', '')
    subnet_ids = [s.strip() for s in subnet_ids_str.split(',') if s.strip()]
    if not subnet_ids:
        logger.info(
            "No subnet IDs found in NodeClaim; defaulting to allow"
        )
        return admission_response(uid, True)

    try:
        throttle_percent = float(
            os.getenv('THROTTLE_AT_PERCENT', '10')
        )
    except ValueError:
        logger.error(
            "Invalid THROTTLE_AT_PERCENT value; defaulting to 10"
        )
        throttle_percent = 10.0

    logger.info(
        "Throttle threshold configured at %.1f%% of total IPs",
        throttle_percent
    )

    # Validate all subnet IDs in the selector
    failed_subnets = []
    for subnet_id in subnet_ids:
        try:
            total, available = describe_subnet(subnet_id)
        except Exception:  # pylint: disable=broad-except
            logger.error(
                "Error fetching subnet info for %s",
                subnet_id,
                exc_info=True
            )
            return admission_response(
                uid, False, f"Error querying subnet {subnet_id}"
            )

        threshold = total * (throttle_percent / 100.0)
        logger.info(
            "Subnet %s: available=%d, threshold=%.1f",
            subnet_id, available, threshold
        )

        if available < threshold:
            percent_free = (available / total) * 100.0 if total else 0
            failed_subnets.append(
                f"{subnet_id} ({available} IPs, {percent_free:.1f}%)"
            )

    if failed_subnets:
        message = (
            "One or more subnets in NodeClaim have too few available IPs: " +
            ", ".join(failed_subnets)
        )
        logger.warning(message)
        return admission_response(uid, False, message)

    logger.info("All subnets in NodeClaim have sufficient IPs")
    return admission_response(uid, True)


def admission_response(uid, allowed, message=None):
    """
    Build the AdmissionReview response.
    """
    logger.debug(
        "Building admission response: uid=%s, allowed=%s, message=%s",
        uid, allowed, message
    )
    response = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": allowed,
        }
    }
    if message and not allowed:
        response['response']['status'] = {
            "code": 400,
            "message": message
        }
    return jsonify(response)


if __name__ == '__main__':
    port = int(os.getenv('PORT', str(DEFAULT_PORT)))
    logger.info(
        "Starting IP throttle webhook on port %d | log level=%s",
        port, LOG_LEVEL
    )
    # In production, TLS should be terminated by Kubernetes Ingress/Service
    app.run(host='0.0.0.0', port=port)
