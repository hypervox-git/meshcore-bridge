# Flask routes for the MeshCore bridge.
from flask import jsonify, request, render_template

from config import (SERIAL_PORT, GATEWAY_LAT, GATEWAY_LON, MAX_TRAIL, NODE_DB_FILE,
                    POS_HIST_FILE, POS_HIST_DAYS, MQTT_BROKER, MQTT_PORT, MQTT_TOPIC,
                    HA_DISCOVERY, ALIAS_FILE)
from bridge import state
from bridge.state import (node_db, message_log, aliases, geo_cache, mqtt_state,
                          pos_history, position_trails, save_json)
from bridge.extensions import app, socketio
from bridge.util import get_sysinfo, reverse_geocode
from bridge import meshcore_io as mc
from bridge import testmode
from bridge import repeater_cli, repeater_test


@app.route("/")
def index():
    return render_template("index.html", serial_port=SERIAL_PORT,
                           gateway_lat=GATEWAY_LAT, gateway_lon=GATEWAY_LON,
                           trail_max=MAX_TRAIL)


@app.route("/api/sysinfo")
def api_sysinfo():
    return jsonify(get_sysinfo())


@app.route("/api/nodes")
def api_nodes():
    # node_db (populated from MeshCore contacts/adverts) rendered in the shape the
    # dashboard/map expect.
    out = {}
    for nid, n in node_db.items():
        node = {"num": 0,
                "user": {"id": nid, "longName": n.get("name", ""), "hwModel": str(n.get("hw", ""))},
                "snr": n.get("snr"), "lastHeard": n.get("lastSeen")}
        if n.get("lat") is not None and n.get("lon") is not None:
            node["position"] = {"latitudeI": int(n["lat"] * 1e7),
                                "longitudeI": int(n["lon"] * 1e7)}
        for k in ("rssi", "pathloss", "pathloss_tx_dbm", "linkTs"):
            if n.get(k) is not None:
                node[k] = n[k]
        dm = {}
        if n.get("battery") is not None:
            dm["batteryLevel"] = n["battery"]
        if dm:
            node["deviceMetrics"] = dm
        out[nid] = node
    return jsonify(out)


@app.route("/api/nodedb")
def api_nodedb():
    return jsonify(node_db)


@app.route("/api/nodedb/flush", methods=["POST"])
def api_nodedb_flush():
    try:
        node_db.clear()
        save_json(NODE_DB_FILE, node_db)
        position_trails.clear()
        geo_cache.clear()
        mqtt_state.clear()
        pos_history.clear()
        save_json(POS_HIST_FILE, pos_history)
        socketio.emit("nodedb_flushed", {}, namespace="/")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/messages")
def api_messages():
    return jsonify(message_log)


@app.route("/api/mqtt")
def api_mqtt():
    return jsonify({"connected": state.mqtt_connected,
                    "broker": f"{MQTT_BROKER}:{MQTT_PORT}",
                    "topic_root": MQTT_TOPIC, "ha_discovery": HA_DISCOVERY,
                    "nodes": mqtt_state})


@app.route("/api/poshist")
def api_poshist():
    node = request.args.get("node")
    if node:
        return jsonify({"node": node, "history": pos_history.get(node, []),
                        "retention_days": POS_HIST_DAYS})
    return jsonify({nid: len(h) for nid, h in pos_history.items()})


@app.route("/api/send", methods=["POST"])
def api_send():
    try:
        data = request.get_json()
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Empty"})
        to = data.get("to")             # contact name/key -> DM
        channel = int(data.get("channel", 0))
        if to:
            mc.send_dm(to, text)
        else:
            mc.send_channel(channel, text)
        from bridge.state import append_message
        append_message({"ts": __import__("time").time() * 1000, "from": "[me]",
                        "text": text, "channel": channel, "sent": True})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/telemetry", methods=["POST"])
def api_telemetry():
    try:
        dst = (request.get_json() or {}).get("to")
        if not dst:
            return jsonify({"ok": False, "error": "no target"})
        mc.request_telemetry(dst)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---- neighbour discovery / test mode ------------------------------------------

def _guard(fn):
    """Run a testmode call, always answering with JSON (errors never leak a 500 page)."""
    try:
        return jsonify(fn())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e) or e.__class__.__name__})


@app.route("/api/repeater")
def api_repeater():
    import config
    if getattr(config, "SERIAL_MODE", "companion") != "repeater":
        return jsonify({"mode": "companion"})
    return jsonify(repeater_cli.snapshot())


@app.route("/api/rtest/status")
def api_rtest_status():
    return _guard(repeater_test.view)


@app.route("/api/rtest/start", methods=["POST"])
def api_rtest_start():
    d = request.get_json(silent=True) or {}
    return _guard(lambda: repeater_test.start(d.get("minutes"), d.get("offline"), d.get("assumed_tx_dbm")))


@app.route("/api/rtest/discover", methods=["POST"])
def api_rtest_discover():
    return _guard(repeater_test.discover)


@app.route("/api/rtest/stop", methods=["POST"])
def api_rtest_stop():
    return _guard(repeater_test.stop)


@app.route("/api/test/status")
def api_test_status():
    return _guard(testmode.status)


@app.route("/api/test/targets")
def api_test_targets():
    return _guard(lambda: {"ok": True, "targets": testmode.list_targets()})


@app.route("/api/neighbors/discover", methods=["POST"])
def api_neighbors_discover():
    d = request.get_json(silent=True) or {}
    return _guard(lambda: testmode.discover(d.get("types"), d.get("wait_s", 8)))


@app.route("/api/neighbors/remote", methods=["POST"])
def api_neighbors_remote():
    d = request.get_json(silent=True) or {}
    return _guard(lambda: testmode.neighbours_of(d.get("to", ""), d.get("password", "")))


@app.route("/api/test/link/start", methods=["POST"])
def api_test_link_start():
    d = request.get_json(silent=True) or {}
    return _guard(lambda: testmode.start_link_test(d.get("to", ""), d.get("password", "")))


@app.route("/api/test/link/stop", methods=["POST"])
def api_test_link_stop():
    return _guard(testmode.stop_link_test)


@app.route("/api/test/trace", methods=["POST"])
def api_test_trace():
    d = request.get_json(silent=True) or {}
    return _guard(lambda: testmode.traceroute(d.get("to", "")))


@app.route("/api/aliases", methods=["GET"])
def api_aliases_get():
    return jsonify(aliases)


@app.route("/api/aliases", methods=["POST"])
def api_aliases_post():
    data = request.get_json()
    nid = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    if nid:
        if name:
            aliases[nid] = name
        elif nid in aliases:
            del aliases[nid]
        save_json(ALIAS_FILE, aliases)
        socketio.emit("aliases", aliases, namespace="/")
    return jsonify({"ok": True})


@app.route("/api/geocode")
def api_geocode():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        key = f"{round(lat,3)},{round(lon,3)}"
        if key in geo_cache and geo_cache[key]:
            return jsonify({"name": geo_cache[key]})
        return jsonify({"name": reverse_geocode(lat, lon)})
    except Exception as e:
        return jsonify({"name": None, "error": str(e)})
