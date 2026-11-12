# Image de la passerelle Qraft (API REST/SSE + MCP).
# Construction depuis la racine du dépôt : docker build -f deploy/Dockerfile -t qraft .

FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY packages ./packages
RUN python -m venv /opt/qraft \
 && /opt/qraft/bin/pip install ./packages/core[duckdb,arrow,claude,local] ./packages/server[mcp,arrow] ./packages/qraft-eval[duckdb]

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 qraft
COPY --from=build /opt/qraft /opt/qraft
COPY examples /app/examples
ENV PATH="/opt/qraft/bin:${PATH}" PYTHONUNBUFFERED=1
WORKDIR /app
USER qraft
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health')"
# QRAFT_DB_URL : URL SQLAlchemy d'un compte en lecture seule. QRAFT_SEMANTIC : couche sémantique.
CMD ["sh", "-c", "qraft serve --host 0.0.0.0 --port 8080 ${QRAFT_SEMANTIC:+--semantic $QRAFT_SEMANTIC} ${QRAFT_PACK:+--pack $QRAFT_PACK} --model ${QRAFT_MODEL:-none}"]
