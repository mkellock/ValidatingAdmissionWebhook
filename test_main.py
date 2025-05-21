# pylint: disable=too-few-public-methods
"""
Tests for the AWS subnet admission webhook.
"""

import pytest
from main import app, subnet_cache


@pytest.fixture(autouse=True)
def clear_cache():
    """
    Ensure cache is clean before and after each test.
    """
    subnet_cache.clear()
    yield
    subnet_cache.clear()


@pytest.fixture
def client_fixture(monkeypatch):
    """
    Provides a test client with a stubbed EC2 client and environment variable.
    """
    # Stub boto3 EC2 client
    class DummyEC2:
        """
        A dummy EC2 client for testing subnet validation logic.
        """
        def __init__(self):
            """
            Dummy init to satisfy pylint's public methods check.
            """

        def describe_subnets(self, SubnetIds):
            """
            Return a dummy subnet description.
            SubnetIds is unused but required for mocking signature.
            """
            # pylint: disable=unused-argument
            return {
                'Subnets': [{
                    'CidrBlock': '10.0.0.0/24',
                    'AvailableIpAddressCount': 50
                }]
            }
    monkeypatch.setenv('THROTTLE_AT_PERCENT', '10')
    monkeypatch.setattr('main.ec2', DummyEC2())
    return app.test_client()


def admission_request(uid, subnet_ids):
    """
    Build a dummy AdmissionReview request for a Karpenter NodeClaim.
    subnet_ids: list or str of subnet IDs (will be joined with ',').
    """
    if isinstance(subnet_ids, list):
        subnet_ids_str = ",".join(subnet_ids)
    else:
        subnet_ids_str = subnet_ids
    return {
        'kind': 'AdmissionReview',
        'apiVersion': 'admission.k8s.io/v1',
        'request': {
            'uid': uid,
            'object': {
                'spec': {
                    'subnetSelector': {
                        'aws-ids': subnet_ids_str
                    }
                }
            }
        }
    }


def test_validate_allows_when_above_threshold(client_fixture):
    """
    Test that validation allows when available IPs are above the threshold.
    """
    req = admission_request('1', 'subnet-123')
    resp = client_fixture.post('/validate', json=req)
    data = resp.get_json()
    assert data['response']['allowed'] is True


def test_validate_denies_when_below_threshold(client_fixture, monkeypatch):
    """
    Test that validation denies when available IPs are below the threshold.
    """
    monkeypatch.setenv('THROTTLE_AT_PERCENT', '80')
    req = admission_request('2', 'subnet-abc')
    resp = client_fixture.post('/validate', json=req)
    data = resp.get_json()
    assert data['response']['allowed'] is False
    assert (
        'subnets in NodeClaim have too few available IPs'
        in data['response']['status']['message']
    )
