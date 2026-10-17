#!/usr/bin/env python3
"""Forced SSH command on Proxmox: leases for one fixed authenticator VM only."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

VMID='140'
STATE=Path('/run/padel-auth-power')
LEASE_SECONDS=180
IDLE_SECONDS=60

def parse_command(command):
    parts=command.split()
    if parts in [['status'],['sweep']]:return parts[0],None
    if len(parts)==2 and parts[0] in {'ensure','heartbeat','release'} and re.fullmatch('[a-f0-9]{32}',parts[1]):
        return parts[0],parts[1]
    raise ValueError('Unsupported authenticator power operation')

def qm(*args):
    return subprocess.run(['/usr/sbin/qm',*args],check=True,capture_output=True,text=True,timeout=150).stdout

def running():return qm('status',VMID).strip()=='status: running'

def main(command):
    action,ident=parse_command(command)
    if action=='sweep' and os.environ.get('SSH_ORIGINAL_COMMAND'):
        raise ValueError('Unsupported authenticator power operation')
    STATE.mkdir(mode=0o700,exist_ok=True)
    with (STATE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        now=time.time();path=STATE/'leases.json'
        try:state=json.loads(path.read_text())
        except (OSError,ValueError):state={'leases':{},'idle_since':now}
        leases={key:until for key,until in state.get('leases',{}).items() if until>now}
        if action in {'ensure','heartbeat'}:
            leases[ident]=now+LEASE_SECONDS
            state['idle_since']=now
            if action=='ensure' and not running():qm('start',VMID)
            leases[ident]=time.time()+LEASE_SECONDS
            state['idle_since']=time.time()
        elif action=='release':
            leases.pop(ident,None)
            if not leases:state['idle_since']=now
        state['leases']=leases
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.chmod(0o600);temporary.replace(path)
        active=running()
        if action=='sweep' and active and not leases and now-state.get('idle_since',now)>=IDLE_SECONDS:
            # Graceful shutdown only: a lost SSH client must not corrupt Android userdata.
            qm('shutdown',VMID,'--timeout','45')
            active=running()
        print(json.dumps({'status':'running' if active else 'stopped','leases':len(leases)}),flush=True)

if __name__=='__main__':
    try:main(os.environ.get('SSH_ORIGINAL_COMMAND') or ' '.join(sys.argv[1:]))
    except Exception:
        print(json.dumps({'error':'Authenticator power operation failed'}),flush=True)
        sys.exit(1)
