# Entrypoint (threading async mode — no eventlet; MeshCore runs its asyncio loop
# in a dedicated thread).
from config import WEB_PORT
from bridge import state, mqtt_bridge, meshcore_io
from bridge import web  # noqa: F401 — importing registers the Flask routes
from bridge.extensions import app, socketio


def main():
    state.load_all()
    mqtt_bridge.start()
    meshcore_io.start()
    socketio.run(app, host="0.0.0.0", port=WEB_PORT, debug=False,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
