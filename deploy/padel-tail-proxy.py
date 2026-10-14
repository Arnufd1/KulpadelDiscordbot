"""Loopback-only bridge from verified Tailscale Serve to the guest's admin proof route."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

CONFIG = json.loads(Path('/etc/padel-tail-proxy.json').read_text())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path != '/admin/verify' or len(self.path)>1024:
            self.send_error(404)
            return
        headers = {'Host':'sigma-minipc.tailad80cf.ts.net:8443',
                   'X-Padel-Tail-Secret':CONFIG['secret'],
                   'Tailscale-User-Login':self.headers.get('Tailscale-User-Login','')}
        connection = http.client.HTTPConnection(CONFIG['guest'],8090,timeout=15)
        try:
            connection.request('GET',self.path,headers=headers)
            response = connection.getresponse()
            body = response.read(16384)
            location = response.getheader('Location','')
            if location and not location.startswith('https://padel.sigmadvisor.com/admin/callback?'):
                self.send_error(502)
                return
            self.send_response(response.status)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            if location:
                self.send_header('Location',location)
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(502)
        finally:
            connection.close()

    def log_message(self,*args):
        pass  # Never log single-use proof tickets.


ThreadingHTTPServer(('127.0.0.1',8191),Handler).serve_forever()
