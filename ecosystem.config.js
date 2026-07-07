// PM2 process definition for forge deployment.
// Runs the installed entry point over streamable-http bound to loopback. No secrets here —
// the server holds no credentials; its only write surface is doc-sync.yml + the docs cache.
// Deploy (sysadmin): create /opt/venvs/doc-cache-mcp, `pip install .`, then start this.
module.exports = {
  apps: [{
    name: "doc-cache-mcp",
    script: "/opt/venvs/doc-cache-mcp/bin/doc-cache-mcp",
    interpreter: "none",
    env: {
      LOG_LEVEL: "INFO",
      DOC_CACHE_MCP_TRANSPORT: "http",
      DOC_CACHE_MCP_HOST: "127.0.0.1",
      DOC_CACHE_MCP_PORT: "8503",
      DOC_CACHE_MCP_DOCSYNC_PATH: "/home/ted/scripts/doc-sync.py",
      DOC_CACHE_MCP_CONFIG_PATH: "/home/ted/docs/doc-sync.yml",
      DOC_CACHE_MCP_ALLOWLIST_PATH: "/home/ted/repos/gitea/host-forge-scripts/doc-cache-allowlist.yml",
      // Optional telemetry (off unless set):
      // OTEL_EXPORTER_OTLP_ENDPOINT: "http://127.0.0.1:4317",
    },
    restart_delay: 5000,
    max_restarts: 10,
    watch: false,
  }]
};
