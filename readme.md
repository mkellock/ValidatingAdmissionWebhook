# IP Throttle Validating Admission Webhook

This webhook prevents Karpenter (on EKS) from provisioning nodes into AWS subnets that have low available IP addresses.

## Features

- Validates `CREATE` operations for `nodes` via a Kubernetes ValidatingWebhook.
- Queries AWS EC2 Subnet API for `AvailableIpAddressCount`.
- Caches subnet queries for 15 seconds to reduce AWS API calls.
- Rejects node creation when available IP count falls below a configurable threshold.
- Comprehensive logging for debugging and observability.

## Configuration

### Environment Variables

- `THROTTLE_AT_PERCENT` (optional): Percentage threshold of free IPs below which node creation is denied. Default: `10`.
- `LOG_LEVEL` (optional): Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Default: `INFO`.
- `PORT` (optional): Port for the Flask app. Default: `8443`.

### TLS Certificate & caBundle

1. Generate a TLS certificate and key for the webhook server:

   ```bash
   openssl req -newkey rsa:2048 -nodes -keyout tls.key \
       -x509 -days 365 -out tls.crt -subj "/CN=throttle.ip.karpenter.aws"
   ```

2. Create a Kubernetes secret in the `kube-system` namespace:

   ```bash
   kubectl -n kube-system create secret tls ip-throttle-webhook-tls \
     --cert=tls.crt --key=tls.key
   ```

3. Extract the CA bundle for the `ValidatingWebhookConfiguration`:

   ```bash
   export CABUNDLE=$(kubectl get secret ip-throttle-webhook-tls \
     -n kube-system -o go-template='{{index .data "tls.crt"}}')
   ```

4. Replace `<CA_BUNDLE>` in `deploy/webhook.yaml` with the value of `${CABUNDLE}`.

### Deploying to Kubernetes

```bash
kubectl apply -f deploy/webhook.yaml
```

## Building and Pushing Docker Image

```bash
docker build -t your-registry/ip-throttle-webhook:latest .
docker push your-registry/ip-throttle-webhook:latest
```

## Testing Locally

1. Install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

   ```bash
   pip install -r requirements.txt
   ```

2. Run unit tests:

   ```bash
   pytest
   ```

## Usage

Once deployed, the webhook will intercept all node creation requests and enforce the configured IP threshold, ensuring your private subnets on EKS don’t exhaust their IP pools.
