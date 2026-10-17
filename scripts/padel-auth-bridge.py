#!/usr/bin/env python3
"""App-side bounded SSH transport. Native credentials travel over stdin only."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

CONFIG=Path('/etc/padelbot-auth/bridge.json')

def install_signal_handlers():
    def interrupted(*_):raise SystemExit(1)
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)

def ssh_args(config,role):
    return ['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','UserKnownHostsFile='+config['known_hosts'],'-o','ConnectTimeout=5',
            '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            '-o','IdentitiesOnly=yes','-i',config[role+'_key'],config[role+'_host']]

def control(config,command):
    result=subprocess.run(ssh_args(config,'control')+[command],stdin=subprocess.DEVNULL,check=True,capture_output=True,text=True,timeout=160)
    answer=json.loads(result.stdout)
    if answer.get('error'):raise RuntimeError('Power control unavailable')
    return answer

def main(mode):
    if mode not in {'approve','enroll','status'}:raise ValueError('Unsupported bridge mode')
    config=json.loads(CONFIG.read_text())
    if mode=='status':
        try:answer=control(config,'status')
        except Exception:answer={'status':'unknown'}
        print(json.dumps(answer),flush=True);return
    ident=uuid.uuid4().hex;stop=threading.Event();remote=None
    def heartbeat():
        while not stop.wait(30):
            try:control(config,'heartbeat '+ident)
            except Exception:pass
    try:
        control(config,'ensure '+ident)
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        deadline=time.monotonic()+120
        while True:
            # Forced command accepts ping only and never starts Android.
            try:
                probe=subprocess.run(ssh_args(config,'worker')+['ping'],stdin=subprocess.DEVNULL,capture_output=True,timeout=8)
                if probe.returncode==0:break
            except subprocess.TimeoutExpired:pass
            if time.monotonic()>=deadline:raise RuntimeError('Authenticator VM unavailable')
            time.sleep(1)
        remote=subprocess.Popen(ssh_args(config,'worker')+[mode],stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=subprocess.DEVNULL)
        try:code=remote.wait(timeout=780)
        except subprocess.TimeoutExpired:
            remote.terminate();remote.wait(timeout=20);raise RuntimeError('Authenticator worker timed out')
        if code:sys.exit(code)
    finally:
        if remote is not None and remote.poll() is None:
            remote.terminate()
            try:remote.wait(timeout=20)
            except subprocess.TimeoutExpired:remote.kill();remote.wait()
        stop.set()
        try:control(config,'release '+ident)
        except Exception:pass

if __name__=='__main__':
    install_signal_handlers()
    try:main(sys.argv[1] if len(sys.argv)==2 else '')
    except Exception:
        print(json.dumps({'error':'De authenticator is tijdelijk niet bereikbaar. Probeer later opnieuw.'}),flush=True)
        sys.exit(1)
