"""Real HTTP transport checks for the operator-configured Goals origin."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from decimal import Decimal

import pytest
from goals_features import fetch_goals


def test_transport_reads_goals_and_rejects_redirects_and_bad_payloads():
    payload={'body':json.dumps([{'target':123.45}]).encode(),'status':200}
    paths=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(payload['status'])
            if payload['status']==302:
                self.send_header('Location','/unexpected')
            self.end_headers()
            self.wfile.write(payload['body'])
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    source=f'http://127.0.0.1:{server.server_port}'
    try:
        assert fetch_goals(source)==[{'target':Decimal('123.45')}]
        assert paths==['/api/goals']
        for body in [b'invalid JSON', b'{}', b'[]'+b' '*2097152, json.dumps([{}]*1001).encode()]:
            payload['body']=body
            with pytest.raises(ValueError):
                fetch_goals(source)
        payload.update(body=b'',status=302)
        with pytest.raises(ValueError,match='redirects'):
            fetch_goals(source)
        assert '/unexpected' not in paths
    finally:
        server.shutdown();server.server_close();worker.join()
