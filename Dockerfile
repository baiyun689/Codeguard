FROM maven:3.9.9-eclipse-temurin-21 AS gateway-build
WORKDIR /build/gateway
COPY services/gateway/pom.xml .
RUN mvn --batch-mode -DskipTests dependency:go-offline
COPY services/gateway/src ./src
RUN mvn --batch-mode -DskipTests package

FROM eclipse-temurin:21-jre
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv git wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/codeguard/venv
ENV PATH="/opt/codeguard/venv/bin:${PATH}"
COPY services/agent/ /app/agent/
RUN pip install --no-cache-dir /app/agent
COPY --from=gateway-build /build/gateway/target/codeguard-gateway.jar /app/codeguard-gateway.jar
WORKDIR /app
EXPOSE 9090
VOLUME ["/app/data", "/tmp/codeguard-jobs"]
ENTRYPOINT ["java", "-jar", "/app/codeguard-gateway.jar"]
