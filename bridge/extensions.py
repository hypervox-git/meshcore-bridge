# Flask app + SocketIO instance (threading mode — MeshCore uses asyncio in its
# own thread, which does not mix with eventlet).
import os
from flask import Flask
from flask_socketio import SocketIO

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__,
            template_folder=os.path.join(BASE, "templates"),
            static_folder=os.path.join(BASE, "static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
