#!/usr/bin/env python3
"""Forced SSH entry point; no shell or arbitrary command is available."""
import json
import os
import sys

command=os.environ.get('SSH_ORIGINAL_COMMAND','')
if command=='ping':
    print('{"ready":true}');sys.exit(0)
if command not in {'approve','enroll'}:
    print('{"error":"Unsupported worker operation"}');sys.exit(1)
os.chdir('/opt/padelbot')
os.environ['PADELBOT_ENV_FILE']=''
os.environ['HOME']='/var/lib/padelbot'
os.environ['ANDROID_USER_HOME']='/var/lib/padelbot/.android'
os.environ['ANDROID_EMULATOR_FEATURES']='-Vulkan'
os.environ['PADELWEB_TELEMETRY_DB']='/var/lib/padelbot/native-metrics.sqlite3'
os.environ['PADELBOT_AUTH_WORKER_MODE']=command
os.execv('/opt/padelbot/.venv/bin/python',['/opt/padelbot/.venv/bin/python','-m','padelbot.remote_auth_worker'])
