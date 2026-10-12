"""Meshtastic serial-to-MQTT bridge (modular). Version 1.13.0."""


def cli_ref():
    """Late import of the repeater CLI module (avoids import cycles)."""
    from bridge import repeater_cli
    return repeater_cli
