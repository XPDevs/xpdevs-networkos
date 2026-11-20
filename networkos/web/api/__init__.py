"""JSON API used by the dashboard front end.

Every endpoint inherits the authentication, CSRF and LAN-only guards applied in
``create_app``; none of them accepts shell text, and all privileged work goes
through ``networkosctl`` with validated arguments.
"""

from __future__ import annotations

from flask import Blueprint

api_bp = Blueprint("api", __name__)

# Imported for their side effect of registering routes on ``api_bp``.
from . import adblock, devices, network, overview, security, settings, system, wireless  # noqa: E402,F401
