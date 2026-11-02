"""Entry point for uvicorn: `uvicorn repolace_api.site.main:app --port 8100`."""

from repolace_api.site.app import create_site_app

app = create_site_app()
