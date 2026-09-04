# Infrastructure — CloudFormation Template

## What It Provisions

`greengrass-ec2.yaml` creates a complete, repeatable infrastructure stack:

| Resource | Purpose |
|----------|---------|
| Security Group | SSH (port 22) from your IP only; no inbound web port |
| EC2 Instance | Amazon Linux 2023, latest AMI via SSM, 30 GB gp3 root volume |
| EC2 IAM Role + Instance Profile | Greengrass v2 auto-provisioning permissions (incl. `iot:CreateJob` for dev tools) |
| Greengrass Token Exchange Role | Runtime role for components — includes Bedrock `InvokeModel` access |
| UserData bootstrap | Installs Java, Python, configures sudoers, downloads & runs the GG installer, signals CloudFormation |

The instance auto-provisions itself as a Greengrass core on boot (creates IoT Thing, certs, role alias, deploys greengrass-cli). After ~5 minutes it's ready for component deployment.

## Reliability Features

- **cfn-signal + CreationPolicy (PT15M)** — If any UserData command fails, the stack reports `CREATE_FAILED` instead of a false `CREATE_COMPLETE`. A bash `trap` ensures the failure signal is sent on any non-zero exit.
- **No manual patches** — The template handles all AL2023-specific quirks (curl-minimal, sudoers, IAM permissions) so a fresh deploy works out of the box.

## Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `VpcId` | Yes | — | VPC to launch in (e.g. your default VPC) |
| `SubnetId` | Yes | — | Public subnet (needs internet access) |
| `KeyName` | Yes | — | Existing EC2 key pair name |
| `InstanceType` | No | `t3.small` | Instance size (≥2 GB RAM) |
| `SshCidrIp` | Yes* | `127.0.0.1/32` | Your public IP/32 for SSH (default blocks all — must change) |
| `GreengrassThingName` | No | `GreengrassBedrock-Demo` | IoT Thing name |
| `GreengrassThingGroupName` | No | `GreengrassBedrock-DemoGroup` | IoT Thing Group |
| `GreengrassVersion` | No | `2.14.0` | Greengrass nucleus version |

*Default `127.0.0.1/32` intentionally blocks external SSH. Set to your IP: `curl -s ifconfig.me`/32.

## Deploy

```bash
# Find your public IP
MY_IP=$(curl -s ifconfig.me)/32

# Deploy the stack
aws cloudformation create-stack \
  --stack-name greengrass-bedrock-demo \
  --template-body file://infra/greengrass-ec2.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-XXXXXXXX \
    ParameterKey=SubnetId,ParameterValue=subnet-XXXXXXXX \
    ParameterKey=KeyName,ParameterValue=my-key-pair \
    ParameterKey=SshCidrIp,ParameterValue="$MY_IP"

# Wait for completion (~5-10 min)
aws cloudformation wait stack-create-complete --stack-name greengrass-bedrock-demo

# Get outputs (public IP, SSH command, etc.)
aws cloudformation describe-stacks --stack-name greengrass-bedrock-demo \
  --query 'Stacks[0].Outputs' --output table
```

## After Deployment

1. SSH into the instance (use the `SshCommand` output).
2. Verify Greengrass is running: `sudo systemctl status greengrass`
3. Copy the demo components to the instance and run `sudo bash deploy.sh`.
4. Access the app via SSH tunnel:
   ```bash
   ssh -L 8080:localhost:8080 -i my-key.pem ec2-user@<PUBLIC_IP>
   # Then open http://localhost:8080 in your browser
   ```

**Note:** Bedrock serverless foundation models (including `amazon.nova-lite-v1:0`) are auto-enabled on all AWS accounts as of Sep 2025. No manual console opt-in step is required — invocation is governed by IAM (the Token Exchange Role's `bedrock:InvokeModel` permission) and region availability.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name greengrass-bedrock-demo
```

Note: The Greengrass installer creates IoT resources (Thing, certificates, policies, role alias) that live outside CloudFormation. To fully clean up, also delete these in the IoT Core console or via CLI.

## AL2023 / Greengrass Specifics (Handled by Template)

The following issues are specific to Amazon Linux 2023 and are handled automatically in the UserData bootstrap:

| Issue | What the Template Does |
|-------|----------------------|
| `curl-minimal` conflicts with `curl` package | Omits `curl` from `dnf install` — `curl-minimal` provides the `curl` binary |
| Nucleus can't run lifecycle scripts as `ggc_user:ggc_group` | Writes `/etc/sudoers.d/greengrass` granting root → ggc_user:ggc_group NOPASSWD |
| `--deploy-dev-tools true` requires `iot:CreateJob` | EC2 role includes `iot:CreateJob` in its IoT policy statement |
| UserData failures silent (false CREATE_COMPLETE) | `cfn-signal` with bash trap + `CreationPolicy` (PT15M timeout) |

## Security Notes

- SSH is restricted to the CIDR you provide — never use `0.0.0.0/0` in production.
- No inbound web ports — the Flask app is accessed only via SSH tunnel (localhost:8080).
- The Token Exchange Role is scoped to Bedrock `InvokeModel` on foundation models, logs, and S3 reads.
- The EC2 provisioning role is scoped to Greengrass-specific IoT and IAM operations only.
