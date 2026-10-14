"""Loopback-only bridge from verified Tailscale Serve to the guest's admin proof route."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

CONFIG = json.loads(Path('/etc/padel-tail-proxy.json').read_text())


def peer_identity(address):
    """Serve overwrites X-Forwarded-For with its actual Tailscale TCP peer.

    Tagged nodes have no human identity header. Resolve their stable node ID
    with the local daemon; only explicitly configured administrator devices qualify.
    """
    ip=ipaddress.ip_address(address)
    if not any(ip in net for net in (ipaddress.ip_network('100.64.0.0/10'),ipaddress.ip_network('fd7a:115c:a1e0::/48')) if ip.version==net.version):
        raise ValueError('Not a Tailscale peer')
    result=subprocess.run(['/usr/bin/tailscale','whois','--json',str(ip)],capture_output=True,text=True,timeout=5,check=True)
    identity=json.loads(result.stdout)
    node=identity.get('Node') or {}
    node_id=node.get('StableID','')
    approved_node=node_id if node_id in CONFIG.get('admin_node_ids',[]) and 'tag:admin' in node.get('Tags',[]) else ''
    return (identity.get('UserProfile') or {}).get('LoginName',''),approved_node


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path != '/admin/verify' or len(self.path)>1024:
            self.send_error(404)
            return
        try:
            login,node=peer_identity(self.headers.get('X-Forwarded-For',''))
        except Exception:
            self.send_error(403)
            return
        headers = {'Host':'sigma-minipc.tailad80cf.ts.net:8443',
                   'X-Padel-Tail-Secret':CONFIG['secret'],
                   'Tailscale-User-Login':login,'X-Padel-Tail-Node':node}
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
