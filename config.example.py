# Copy to config.py and edit. config.py is git-ignored.
SERIAL_PORT     = "/dev/serial/by-id/usb-CHANGEME"   # MeshCore node flashed as a SERIAL companion
BAUD_RATE       = 115200
MESHCORE_DEBUG  = False        # True logs raw event payloads (use to confirm field mapping)
MQTT_BROKER     = "127.0.0.1"
MQTT_PORT       = 1883
MQTT_TOPIC      = "meshcore"
MQTT_USER       = None
MQTT_PASS       = None
WEB_PORT        = 5001
MSG_LOG_FILE    = "meshcore_messages.json"
NODE_DB_FILE    = "meshcore_nodes.json"
ALIAS_FILE      = "meshcore_aliases.json"
POS_HIST_FILE   = "meshcore_poshist.json"
MAX_LOG         = 500
MAX_TRAIL       = 20
HA_DISCOVERY    = True
HA_PREFIX       = "homeassistant"
GATEWAY_LAT     = 0.0
GATEWAY_LON     = 0.0
POS_HIST_DAYS   = 28
POS_HIST_MAX    = 10000

# --- Neighbours / Test mode (all optional; defaults shown) ---------------------
# Legal duty cycle for your sub-band. None = auto from the node's frequency
# (869.400-869.650 MHz -> 10%, anything else -> 1%). Set explicitly to override,
# e.g. 0.01 for a 1% band.
DUTY_CYCLE_LIMIT      = None
# Fraction of that limit the test features may use, per transmitter (our node AND
# the target), so normal mesh traffic keeps the rest. Capped at 0.9.
TEST_DUTY_SHARE       = 0.25
TEST_MIN_INTERVAL     = 10     # s, floor between live-test radio polls
TEST_LOCAL_POLL       = 2.0    # s, local noise-floor/RSSI refresh (serial only, no airtime)
TEST_MAX_MINUTES      = 30     # live test auto-stops after this long
DISCOVER_MIN_INTERVAL = 30     # s between neighbour discoveries
TRACE_MIN_INTERVAL    = 20     # s between traceroutes
NEIGHBOURS_MAX        = 16     # rows requested from a repeater's neighbour table
# Default guest/admin password used when you leave the password box empty. Repeaters
# only answer status/neighbour requests after a login. Keep it in config.py only.
REPEATER_PASSWORD     = ""
