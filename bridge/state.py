# Shared runtime state + JSON persistence. All mutable stores are module
# attributes; load_all() mutates them IN PLACE so `from bridge.state import x`
# stays valid after load. The two reassigned flags are accessed as
# state.mqtt_connected / state.serial_connected elsewhere.
import time
import json
import os
import threading

from config import MSG_LOG_FILE, NODE_DB_FILE, ALIAS_FILE, POS_HIST_FILE, MAX_LOG

mqtt_connected = False
serial_connected = False
iface_ref = [None]
message_log = []
node_db = {}
aliases = {}
local_node_id = [None]
geo_cache = {}
position_trails = {}
pos_history = {}
mqtt_state = {}
port_lock = threading.Lock()
reconnecting = [False]
last_serial_error = [None]


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Save error {path}: {e}")


def load_all():
    data = load_json(MSG_LOG_FILE, [])
    message_log.clear()
    message_log.extend(data)
    for store, path in ((node_db, NODE_DB_FILE), (aliases, ALIAS_FILE),
                        (pos_history, POS_HIST_FILE)):
        store.clear()
        store.update(load_json(path, {}))


def append_message(entry):
    message_log.append(entry)
    if len(message_log) > MAX_LOG:
        message_log.pop(0)
    save_json(MSG_LOG_FILE, message_log[-MAX_LOG:])


def update_node_db(node_id, data):
    if node_id not in node_db:
        node_db[node_id] = {}
    node_db[node_id].update(data)
    node_db[node_id]["lastSeen"] = int(time.time())
    save_json(NODE_DB_FILE, node_db)
