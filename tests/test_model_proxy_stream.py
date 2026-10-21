"""A partial upstream response must fail promptly for every downstream client."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from lg_bench import model_proxy


def test_incomplete_upstream_closes_downstream(monkeypatch, tmp_path):
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.send_header('transfer-encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'7\r\ndata:x\n\r\n')
            self.wfile.flush()
            # Simulate an upstream which stalls after a valid first chunk.
            time.sleep(0.5)
            self.close_connection = True

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), model_proxy.Handler)
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('LG_OPENAI_BASE_URL', f'http://127.0.0.1:{upstream.server_port}')
    monkeypatch.setenv('LG_OPENAI_API_KEY', 'test-placeholder')
    monkeypatch.setenv('LG_PROXY_ALLOWED_MODELS', 'test-model')
    monkeypatch.setenv('LG_PROXY_TIMEOUT', '0.05')
    monkeypatch.setenv('LG_PROXY_LOG_DIR', str(tmp_path))
    client = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=0.3)
    try:
        client.request('POST', '/openai/chat/completions',
                       json.dumps({'model': 'test-model', 'stream': True}),
                       {'content-type': 'application/json'})
        response = client.getresponse()
        assert response.status == 200
        try:
            response.read()
        except http.client.IncompleteRead as exc:
            assert exc.partial == b'data:x\n'
        else:
            raise AssertionError('A failed stream was falsely reported complete')
        entry = json.loads((tmp_path/'requests.jsonl').read_text().splitlines()[-1])
        assert entry['error'] == 'TimeoutError'
        assert entry['error_phase'] == 'upstream_read'
        assert entry['response_complete'] is False
        assert entry['request_id'] and entry['started_at']
    finally:
        client.close()
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
