"""Apprise plugin package — multi-channel unified foundation (P0-2).

Library API registration approach: importing this package completes the registration of the qscard:// scheme
(the @notify decorator self-registers to N_MGR when the module is imported).
APPRISE_PLUGIN_PATH is a CLI-only environment variable; in-library we do not rely on path scanning.

Even when apprise is not installed, this package can still be imported (direct calls to send_qscard are available); the scheme simply won't be registered.
"""
from delivery_layer.apprise_plugins import qscard_plugin

__all__ = ["qscard_plugin"]
