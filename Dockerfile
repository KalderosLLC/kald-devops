FROM ubuntu:24.04

LABEL maintainer="Kalderos LLC <devops@kalderos.com>"
LABEL description="Devops tools image with kald-devops, Terraform, Flyway, and infrastructure CLIs"

ENV DEBIAN_FRONTEND=noninteractive

# Update system and install base + build dependencies
RUN apt-get update && \
    apt-get install -y \
        python3-full \
        python3-pip \
        python3-dev \
        build-essential \
        libssl-dev \
        libffi-dev \
        curl \
        wget \
        git \
        jq \
        unzip \
        tar \
        gzip \
        ca-certificates \
        lsb-release \
        openssh-client && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Terraform binary directly (hardcoded stable version, auto-detect architecture)
RUN TERRAFORM_VERSION="1.9.5" && \
    ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then TERRAFORM_ARCH="arm64"; else TERRAFORM_ARCH="amd64"; fi && \
    curl -fsSL https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TERRAFORM_ARCH}.zip -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    rm /tmp/terraform.zip && \
    terraform version

# Install Flyway (x86_64 - GitHub Actions runs on x86)
RUN FLYWAY_VERSION="9.22.3" && \
    mkdir -p /opt && \
    curl -fsSL https://repo1.maven.org/maven2/org/flywaydb/flyway-commandline/${FLYWAY_VERSION}/flyway-commandline-${FLYWAY_VERSION}-linux-x64.tar.gz -o /tmp/flyway.tar.gz && \
    tar xz -C /opt -f /tmp/flyway.tar.gz && \
    rm /tmp/flyway.tar.gz && \
    ln -s /opt/flyway-${FLYWAY_VERSION}/flyway /usr/local/bin/flyway && \
    flyway --version

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

# Install GitHub CLI
RUN apt-get update && \
    apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    gh version

# Install Azure CLI via pip (more reliable than apt)
RUN python3 -m pip install azure-cli && \
    az version

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
CMD ["-c", "echo 'Kalderos DevOps Tools (Ubuntu 24.04 LTS)' && bash"]
