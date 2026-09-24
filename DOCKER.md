# DevOps Tools Docker Image

The `devops-tools` Docker image includes kald-devops and all infrastructure tools needed for deployment workflows.

## Image Details

- **Base Image**: Ubuntu 24.04 LTS (same as GitHub Actions runners)
- **Registry**: ghcr.io/kalderosllc/kald-devops/devops-tools
- **Included Tools**:
  - kald-devops CLI
  - Terraform
  - Flyway (database migrations)
  - Azure CLI
  - GitHub CLI
  - PostgreSQL client
  - MySQL client
  - SQLite
  - curl, jq, git, wget
  - Python 3 & pip

## Usage

### In GitHub Actions (GitHub Runners)

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/kalderosllc/kald-devops/devops-tools:0.1.2
      credentials:
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - run: terraform plan
      - run: kald-devops list_pipelines --json
```

### In GitHub Actions (Self-Hosted Runners)

```yaml
jobs:
  deploy:
    runs-on: [self-hosted, linux, rocky9]
    container:
      image: ghcr.io/kalderosllc/kald-devops/devops-tools:latest
    steps:
      - uses: actions/checkout@v4
      - run: terraform apply -auto-approve
```

### Local Development

```bash
# Pull the image
docker pull ghcr.io/kalderosllc/kald-devops/devops-tools:latest

# Run interactively
docker run -it \
  -v $(pwd):/workspace \
  -v ~/.azure:/home/devops/.azure \
  -v ~/.ssh:/home/devops/.ssh \
  ghcr.io/kalderosllc/kald-devops/devops-tools:latest

# Run a specific command
docker run --rm \
  -v $(pwd):/workspace \
  ghcr.io/kalderosllc/kald-devops/devops-tools:latest \
  terraform plan
```

## Image Tags

Images are tagged with:
- `latest` - most recent stable release
- `major.minor.patch` - semantic version (e.g., 0.1.2)
- `major.minor` - latest patch of minor release (e.g., 0.1)
- `major` - latest minor/patch of major release (e.g., 0)
- `image-vmajor.minor.patch` - image-only releases (Terraform updates, CVE fixes, etc.)
- `sha-<commit>` - specific commit build

## Building Locally

```bash
# Build the image
docker build -t devops-tools:local .

# Run locally built image
docker run -it -v $(pwd):/workspace devops-tools:local

# Run a specific command
docker run --rm -v $(pwd):/workspace devops-tools:local terraform --version
```

**Note:** The Dockerfile downloads tool binaries directly (Terraform, Flyway) which works reliably both locally and in GitHub Actions, even behind corporate proxies with SSL interception.

## Version Strategy

The image is published with each kald-devops release:
- `v0.1.2` → `devops-tools:0.1.2`
- `v0.2.0` → `devops-tools:0.2.0`

For infrastructure tool updates (Terraform, Flyway, Azure CLI), publish image-only versions:
- `image-v0.1.2.1` → `devops-tools:image-v0.1.2.1`

This allows updating tools without forcing a kald-devops release.

## Environment Variables

Common environment variables for use in workflows:

```bash
# Azure authentication
AZURE_SUBSCRIPTION_ID=<subscription-id>
AZURE_CLIENT_ID=<client-id>
AZURE_CLIENT_SECRET=<client-secret>
AZURE_TENANT_ID=<tenant-id>

# GitHub authentication
GITHUB_TOKEN=<token>

# Terraform backend
TF_BACKEND_CONFIG=backend.tfvars

# Flyway database
FLYWAY_URL=jdbc:postgresql://localhost:5432/mydb
FLYWAY_USER=flyway_user
FLYWAY_PASSWORD=<password>
```

## Security

- Non-root user (`devops`) runs by default
- Container runs with read-only filesystem where possible
- Sensitive credentials passed via environment variables or volume mounts
- Regular image rebuilds to patch security issues

## Troubleshooting

### Image pull fails
```bash
# Ensure authentication
echo $GITHUB_TOKEN | docker login ghcr.io -u <username> --password-stdin

# Pull specific version
docker pull ghcr.io/kalderosllc/kald-devops/devops-tools:0.1.2
```

### Permission denied errors
```bash
# Volume mount permissions - map UID/GID
docker run --user $(id -u):$(id -g) \
  -v $(pwd):/workspace \
  ghcr.io/kalderosllc/kald-devops/devops-tools:latest
```

### Tool version issues
```bash
# Check tool versions in running container
docker run --rm ghcr.io/kalderosllc/kald-devops/devops-tools:latest \
  bash -c "terraform version && flyway -version && az version"
```

## Contributing

Updates to the Dockerfile should be committed to the kald-devops repository. The `publish-image.yml` workflow automatically builds and publishes the image on tagged releases.
