"""MeshCore serial-to-MQTT bridge and web dashboard."""


def cli_ref():
    """Late import of the repeater CLI module (avoids import cycles)."""
    from bridge import repeater_cli
    return repeater_cli
