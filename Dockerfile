FROM maven:3.9.9-eclipse-temurin-21 AS gateway-build
WORKDIR /build/gateway
COPY services/gateway/pom.xml ./pom.xml
COPY services/gateway/shared/pom.xml ./shared/pom.xml
COPY services/gateway/tool-server/pom.xml ./tool-server/pom.xml
COPY services/gateway/ci-webhook/pom.xml ./ci-webhook/pom.xml
COPY services/gateway/llm-proxy/pom.xml ./llm-proxy/pom.xml
RUN mvn --batch-mode -DskipTests dependency:go-offline
COPY services/gateway/ ./
RUN mvn --batch-mode -DskipTests package

FROM eclipse-temurin:21-jre
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv git wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/codeguard/venv
ENV PATH="/opt/codeguard/venv/bin:${PATH}"
COPY services/agent/ /app/agent/
RUN pip install --no-cache-dir /app/agent
COPY --from=gateway-build /build/gateway/ci-webhook/target/codeguard-gateway.jar /app/codeguard-gateway.jar
WORKDIR /app
EXPOSE 8080 9090 9091
VOLUME ["/app/data", "/tmp/codeguard-jobs"]
ENTRYPOINT ["java", "-jar", "/app/codeguard-gateway.jar"]
