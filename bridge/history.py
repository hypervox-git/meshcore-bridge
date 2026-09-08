# Round-robin per-node position history: append a fix, prune anything older than
# the retention window, cap length, dedupe stationary fixes; persisted to JSON.
import time

from config import POS_HIST_FILE, POS_HIST_DAYS, POS_HIST_MAX
from bridge.state import pos_history, save_json


def record_position_history(node_id, lat, lon):
    now = int(time.time())
    hist = pos_history.setdefault(node_id, [])
    if hist and hist[-1][1] == lat and hist[-1][2] == lon:
        return  # skip duplicate (stationary smart-broadcast)
    hist.append([now, lat, lon])
    cutoff = now - POS_HIST_DAYS * 86400
    while hist and hist[0][0] < cutoff:
        hist.pop(0)
    if len(hist) > POS_HIST_MAX:
        del hist[0:len(hist) - POS_HIST_MAX]
    save_json(POS_HIST_FILE, pos_history)
