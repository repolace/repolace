"""The public site process. Imports nothing from `repolace_api.routes` or `repolace_api.ui`."""

from repolace_api.site.app import create_site_app

__all__ = ["create_site_app"]
