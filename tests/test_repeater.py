from bridge import repeater_cli as rc


class FakeSer:
    def __init__(self, replies): self.replies, self.buf, self.sent = replies, b'', []
    def reset_input_buffer(self): self.buf = b''
    def write(self, b):
        line = b.decode().strip(); self.sent.append(line)
        self.buf = ('  -> ' + self.replies[line] + '\r\n').encode()
    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def test_cmd_and_json(monkeypatch):
    monkeypatch.setattr(rc, '_ser', FakeSer({
        'stats-radio': '{"noise_floor":-115,"last_rssi":-120,"last_snr":-6.00,"tx_air_secs":19,"rx_air_secs":62}'}))
    d = rc._json_of(rc._cmd('stats-radio', wait=1, idle=0.05))
    assert d['noise_floor'] == -115 and d['last_snr'] == -6.0


def test_neighbours_parse():
    assert rc.parse_neighbours(['-none-']) == []
    assert rc.parse_neighbours(['AABBCCDD:12:-3.5'])[0]['id'] == 'AABBCCDD'
    assert rc.parse_neighbours(['weird'])[0]['raw'] == 'weird'
