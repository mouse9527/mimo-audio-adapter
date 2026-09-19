"""Standalone mock MiMo server for end-to-end runs without a real API key."""
import base64
import json
import struct
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def pcm(n=2400):
    return b"".join(struct.pack("<h", (i * 97) % 3000 - 1500) for i in range(n))


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        model = req.get("model", "")
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._json(401, {"error": {"message": "missing key"}})

        if "asr" in model:
            return self._json(200, {"choices": [{"message": {
                "role": "assistant", "content": "打开客厅空调"}}]})

        fmt = (req.get("audio") or {}).get("format", "wav")
        raw = pcm()
        body = raw if fmt == "pcm16" else (
            b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
            + b"data" + struct.pack("<I", len(raw)) + raw)

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            step = len(body) // 5 or len(body)
            for i in range(0, len(body), step):
                ev = {"choices": [{"delta": {"audio": {
                    "data": base64.b64encode(body[i:i + step]).decode()}}}]}
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        return self._json(200, {"choices": [{"message": {
            "role": "assistant", "audio": {"data": base64.b64encode(body).decode()}}}]})

    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
