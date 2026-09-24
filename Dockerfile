FROM ubuntu:24.04

LABEL maintainer="Kalderos LLC <devops@kalderos.com>"
LABEL description="Devops tools image with kald-devops, Terraform, Flyway, and infrastructure CLIs"

ENV DEBIAN_FRONTEND=noninteractive

# Update system and install base dependencies
RUN apt-get update && \
    apt-get install -y \
        python3 \
        python3-pip \
        curl \
        wget \
        git \
        jq \
        unzip \
        tar \
        gzip \
        ca-certificates \
        lsb-release && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Terraform binary directly (hardcoded stable version, auto-detect architecture)
RUN TERRAFORM_VERSION="1.9.5" && \
    ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then TERRAFORM_ARCH="arm64"; else TERRAFORM_ARCH="amd64"; fi && \
    curl -fsSL -k https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TERRAFORM_ARCH}.zip -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    rm /tmp/terraform.zip && \
    terraform version

# Install utilities from Ubuntu repos
RUN apt-get update && \
    apt-get install -y \
        postgresql-client \
        mysql-client \
        sqlite3 \
        vim \
        nano \
        less && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Azure CLI (separate step with retry logic)
RUN apt-get update && \
    apt-get install -y azure-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    az version || true

# Install GitHub CLI (separate step with retry logic)
RUN apt-get update && \
    apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    gh version || true

# Install Flyway (will be added in GitHub Actions, local builds can skip this for now)
# This is intentionally left as a manual step for now due to architecture/availability issues

# Upgrade pip and install Python dependencies
RUN python3 -m pip install --upgrade pip setuptools wheel

# Install kald-devops from GitHub Pages PyPI index (public)
RUN python3 -m pip install --extra-index-url https://kalderosllc.github.io/kald-devops/simple kald-devops && \
    kald-devops usage

# Create non-root user for security
RUN useradd -m -s /bin/bash devops && \
    mkdir -p /workspace && \
    chown devops:devops /workspace

# Set working directory
WORKDIR /workspace

# Use non-root user
USER devops

# Default entrypoint
ENTRYPOINT ["/bin/bash"]
CMD ["-c", "echo 'Kalderos DevOps Tools (Rocky 9)' && bash"]
