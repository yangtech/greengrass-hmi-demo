# Deploying the Greengrass Bedrock IPC Demo on a Physical Linux Machine

This guide explains how to set up an AWS IoT Greengrass v2 core on your own
physical Linux machine and deploy the SAME two components you already run on
EC2 (`com.demo.WebApp` and `com.demo.WordCounter`), with the SAME IPC pub/sub
message flow and the SAME Bedrock (Nova Lite) integration.

## Key Idea

The components, recipes, `deploy.sh`, and IPC pub/sub logic are **identical**
on any Greengrass core -- physical or EC2. Only the layer *underneath* the
components differs:

1. **How Greengrass gets installed** -- on EC2 the CloudFormation UserData did
   it automatically; on a physical box you run the installer by hand.
2. **How the box gets AWS credentials** -- on EC2 the instance role gave the
   installer permission to provision; on a physical box you supply credentials
   to the installer yourself (for the one-time provisioning step only).

Once provisioned, the device uses its own X.509 certificate + the **Token
Exchange Service (TES)** to obtain temporary AWS credentials at runtime --
exactly like EC2. No AWS keys are stored on the device after install.

---

## Prerequisites

On the physical machine:

- 64-bit Linux (Amazon Linux 2023, Ubuntu 22.04+, or similar).
- **Java 11+** (JDK) -- the Greengrass nucleus is a Java process.
  - Amazon Linux: `sudo dnf install -y java-11-amazon-corretto-headless`
  - Ubuntu/Debian: `sudo apt-get update && sudo apt-get install -y openjdk-11-jdk`
- **Python 3.9+** and `pip` (components install Flask/boto3/awsiotsdk via pip).
- `unzip` and a working outbound internet connection to:
  - AWS IoT Core endpoints (for provisioning + TES credential exchange), and
  - Amazon Bedrock (`bedrock-runtime`) in your region.

On your workstation / for the install:

- The project files (`greengrass-bedrock-ipc-demo/`) available to copy onto the
  machine (scp, git, or USB).
- **Temporary AWS admin-ish credentials** for the *provisioning step only*
  (see Step 0). These are used once, by the installer, to create the IoT Thing,
  certificate, and Token Exchange Role. They are NOT stored on the device and
  are NOT used at component runtime.

---

## Choose a provisioning mode

**Mode A -- Automatic provisioning + TES (RECOMMENDED, mirrors EC2).**
The machine registers as an IoT Thing with its own X.509 cert and uses the
Token Exchange Role for Bedrock credentials -- identical credential model to
your EC2 core. Requires the machine to reach AWS IoT Core. This guide uses
Mode A.

**Mode B -- Local-only (`--provision false`).**
No cloud IoT registration. Simpler to install, but there is NO TES, so the
WebApp's `boto3.client("bedrock-runtime")` call must get credentials another
way (machine's `~/.aws/credentials` or `AWS_*` env vars). Less secure, no
auto-rotation. See "Appendix: Local-only variant" at the end.

---

## Step 0 -- Provide credentials for the installer (Mode A)

The installer needs AWS credentials with permission to create the IoT Thing,
certificate/policy, and the Token Exchange IAM role/alias. Export them in the
shell you will run the installer from:

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...        # only if using temporary/STS creds
export AWS_REGION=us-east-1         # use the SAME region as your EC2 deploy
```

These are consumed by the installer only. After provisioning, the device
authenticates with its certificate; you can unset them.

> Reuse note: if you point the installer at the SAME Token Exchange Role your
> EC2 stack already created (`GreengrassV2TokenExchangeRole`), the physical
> device inherits the same `bedrock:InvokeModel` permission automatically.
> Multiple core devices can share one role alias / token exchange role -- the
> role is referenced by a role alias in each device's nucleus config, and you
> manage permissions in one place. (If you prefer isolation, create a separate
> role for the physical device with the same Bedrock policy.)

---

## Step 1 -- Create the Greengrass system user and group

```bash
sudo useradd --system --create-home ggc_user
sudo groupadd --system ggc_group 2>/dev/null || true
```

## Step 2 -- (Amazon Linux 2023 ONLY) add the sudoers rule

AL2023's default sudoers does NOT let `root` execute commands as
`ggc_user:ggc_group`. Without this, the nucleus (running as root) cannot drop
privileges to run component lifecycle scripts and every component ends up
BROKEN. Add the rule:

```bash
echo 'root ALL=(ggc_user:ggc_group) NOPASSWD=[REDACTED_PASSWORD] | sudo tee /etc/sudoers.d/greengrass
sudo chmod 440 /etc/sudoers.d/greengrass
sudo visudo -c        # validate syntax
```

(On Ubuntu/Debian this step is typically not required.)

## Step 3 -- (Amazon Linux 2023 ONLY) note on curl

Do NOT `dnf install curl` on AL2023 -- it conflicts with the pre-installed
`curl-minimal` and, under `set -e`, aborts the whole install. The `curl`
binary is already available via `curl-minimal`. (This is the same pitfall that
broke the EC2 UserData; irrelevant on Ubuntu.)

## Step 4 -- Download the Greengrass v2 installer

```bash
cd /tmp
curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-2.14.0.zip -o greengrass.zip
unzip -qo greengrass.zip -d GreengrassInstaller
```

## Step 5 -- Install and auto-provision the core

This creates the IoT Thing, certificate, and (re)uses the Token Exchange Role,
sets up the systemd service, and installs the Greengrass CLI.

```bash
sudo -E java -Droot="/greengrass/v2" -Dlog.store=FILE \
  -jar /tmp/GreengrassInstaller/lib/Greengrass.jar \
  --aws-region us-east-1 \
  --thing-name GreengrassBedrock-PhysicalCore \
  --thing-group-name GreengrassBedrock-DemoGroup \
  --tes-role-name GreengrassV2TokenExchangeRole \
  --tes-role-alias-name GreengrassV2TokenExchangeRoleAlias \
  --provision true \
  --setup-system-service true \
  --deploy-dev-tools true
```

Notes:
- `-E` preserves the exported AWS credentials for the installer (sudo strips
  the environment otherwise).
- Use a DISTINCT `--thing-name` from your EC2 core
  (`GreengrassBedrock-PhysicalCore` vs `GreengrassBedrock-Demo`) so they are
  separate devices.
- Keeping the same `--thing-group-name` lets you target both cores with one
  cloud deployment later if you choose.
- If the Token Exchange Role / alias already exists (created by your EC2
  stack), the installer reuses it -- that is intended.
- `--deploy-dev-tools true` installs the Greengrass CLI (needed by
  `deploy.sh`). This requires the provisioning credentials to allow
  `iot:CreateJob` (already covered by admin-ish install creds).

## Step 6 -- Verify Greengrass is running

```bash
sudo systemctl status greengrass --no-pager | head -20
sudo /greengrass/v2/bin/greengrass-cli --version
```

You should see the service `active (running)` and a CLI version (e.g.
`2.14.0`).

---

## Step 7 -- Copy the components onto the machine

From your workstation (or clone via git):

```bash
scp -r /path/to/greengrass-bedrock-ipc-demo user@<physical-machine-ip>:~/
```

The relevant part is the `components/` directory and `deploy.sh` -- the exact
same files you use on EC2. No edits are needed to the recipes or code.

## Step 8 -- Deploy the two components

On the physical machine:

```bash
cd ~/greengrass-bedrock-ipc-demo
sudo bash deploy.sh
```

`deploy.sh` stages the recipes + artifacts into the layout the Greengrass CLI
expects and creates a LOCAL deployment of `com.demo.WebApp` and
`com.demo.WordCounter`. On first run, the WebApp's install lifecycle runs
`pip install flask boto3 awsiotsdk`, so allow ~30-60s before the port answers.

## Step 9 -- Verify components and the full loop

```bash
# both should be RUNNING
sudo /greengrass/v2/bin/greengrass-cli component list

# healthy startup + IPC subscriptions
sudo tail -30 /greengrass/v2/logs/com.demo.WebApp.log
#   look for: Flask listening on :8080  AND  [IPC] Subscribed to demo/wordcount
sudo tail -30 /greengrass/v2/logs/com.demo.WordCounter.log
#   look for: [WordCounter] Subscribed to demo/answer

# smoke-test the real loop (real Bedrock via TES)
curl -s -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the capital of France?"}'

# confirm the word count flowed back over topic B
sudo tail -10 /greengrass/v2/logs/com.demo.WordCounter.log
```

Expected: `/ask` returns a Nova Lite answer; WordCounter logs the received
answer + a published word count; the WebApp receives that count on
`demo/wordcount`.

## Step 10 -- Access the web app in a browser

- **On the machine itself** (has a desktop): open `http://localhost:8080`.
- **From another machine**: use an SSH tunnel to keep it on localhost (secure
  context, no open web port):
  ```bash
  ssh -L 8080:localhost:8080 user@<physical-machine-ip>
  # then open http://localhost:8080 on your laptop
  ```

---

## EC2 vs Physical -- what changes

| Aspect | EC2 (current) | Physical machine |
|---|---|---|
| Components + recipes | -- | Identical, no changes |
| `deploy.sh` | -- | Identical |
| IPC pub/sub (topics A/B) | -- | Identical |
| Bedrock integration | Nova Lite via TES | Same, via TES (reused role) |
| Greengrass install | Automatic (UserData) | Manual installer (Steps 1-6) |
| Install-time AWS creds | EC2 instance role | You export creds for install only |
| Runtime creds | Token Exchange Role | Same Token Exchange Role (shared) |
| Network | public subnet egress | machine needs outbound internet |
| Thing name | GreengrassBedrock-Demo | GreengrassBedrock-PhysicalCore |

---

## Troubleshooting

- **Components BROKEN on AL2023** -> sudoers rule missing (Step 2). Add
  `/etc/sudoers.d/greengrass`, then redeploy or restart Greengrass.
- **`greengrass-cli: command not found`** -> the core was installed without
  `--deploy-dev-tools true`, or the provisioning creds lacked `iot:CreateJob`.
  Re-run install with dev tools, or deploy the `aws.greengrass.Cli` component.
- **Bedrock AccessDenied** -> the Token Exchange Role lacks `bedrock:InvokeModel`,
  or you're in a different region than expected. Model access itself is
  auto-enabled for serverless models (no console opt-in) since Sep 2025 -- so
  AccessDenied is an IAM/region issue, not a model-access one.
- **Port 8080 not answering** -> first-run `pip install` still in progress;
  watch `com.demo.WebApp.log`.
- **Nucleus cannot reach AWS** -> check outbound internet / DNS / proxy; the
  device needs IoT Core + Bedrock connectivity.

---

## Appendix: Local-only variant (Mode B, `--provision false`)

For a purely local test with no cloud IoT registration:

1. Install with `--provision false` (skip `--tes-role-*` flags):
   ```bash
   sudo -E java -Droot="/greengrass/v2" -Dlog.store=FILE \
     -jar /tmp/GreengrassInstaller/lib/Greengrass.jar \
     --provision false \
     --setup-system-service true
   ```
2. Install the Greengrass CLI locally (dev tools) so `deploy.sh` works, or
   deploy the `aws.greengrass.Cli` component via a local deployment.
3. Because there is NO TES, provide AWS credentials for the Bedrock call
   another way -- e.g. place them in `ggc_user`'s environment or
   `~/.aws/credentials`, since the WebApp component runs as `ggc_user`. This is
   less secure (static keys on the box, no auto-rotation) and is only
   recommended for short-lived local testing.
4. Deploy and test exactly as in Steps 8-10.

> Prefer Mode A for anything beyond a quick local experiment -- it keeps the
> keyless TES credential model you use on EC2.
