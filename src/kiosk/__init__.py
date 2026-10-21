"""Privacy-first AI photo kiosk: session, consent, temporary processing and verifiable deletion.

The package has no AWS imports at module level. AWS adapters live in ``kiosk.aws`` and local
(in-memory) adapters in ``kiosk.local``; ``kiosk.service`` only talks to the ports in ``kiosk.ports``.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
