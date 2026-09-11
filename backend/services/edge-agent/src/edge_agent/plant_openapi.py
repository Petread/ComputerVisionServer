"""OpenAPI export entrypoint; productive dual-server startup uses edge_agent.server."""

from edge_agent.plant import create_plant_app

app = create_plant_app()
