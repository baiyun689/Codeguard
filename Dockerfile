FROM eclipse-temurin:21-jre

# Install Python 3.12 and git
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git && \
    rm -rf /var/lib/apt/lists/*

# Make `python` available (Debian packages provide `python3`)
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1

# Verify python is available
RUN python3 --version || python --version

# Copy Java Gateway
COPY services/gateway/target/codeguard-gateway.jar /app/codeguard-gateway.jar

# Copy and install Python Agent
COPY services/agent/ /app/agent/
WORKDIR /app/agent
RUN pip install --no-cache-dir -e . --break-system-packages

WORKDIR /app
EXPOSE 9090

VOLUME ["/app/data", "/tmp/codeguard-jobs"]

ENTRYPOINT ["java", "-jar", "codeguard-gateway.jar"]
