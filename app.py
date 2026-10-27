# Entrypoint (threading async mode — no eventlet; MeshCore runs its asyncio loop
# in a dedicated thread).
from config import WEB_PORT
import config
from bridge import state, mqtt_bridge, meshcore_io, repeater_cli, repeater_test, publicdir, nbtable, skywatcher, sweep
from bridge import web  # noqa: F401 — importing registers the Flask routes
from bridge.extensions import app, socketio


def main():
    state.load_all()
    mqtt_bridge.start()
    publicdir.start()                 # public node directory (positions of known repeaters)
    skywatcher.start()                # keeps the modelled antenna aim equal to the mount's real bearing
    nbtable.start()                   # persistent neighbour table (+ 24 h public-database sync)
    sweep.load()                      # last antenna sweep, so its results survive a restart
    if getattr(config, 'SERIAL_MODE', 'companion') == 'repeater':
        repeater_test.install_exit_hooks()
        repeater_cli.hooks.append(nbtable.on_sample)   # heard neighbours -> table
        repeater_cli.start()          # CLI polling of a repeater node + time-boxed test mode
    else:
        meshcore_io.start()
    socketio.run(app, host="0.0.0.0", port=WEB_PORT, debug=False,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
