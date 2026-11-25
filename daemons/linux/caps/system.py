"""Read-only Linux host diagnostics."""
import os
import platform
import shutil
import socket
import time

CAPABILITY = 'cap:linux.system'


def status(args, ctx):
    disk = shutil.disk_usage(os.path.expanduser('~'))
    with open('/proc/uptime', encoding='ascii') as stream:
        uptime = float(stream.read().split()[0])
    return {'ok': True, 'hostname': socket.gethostname(), 'platform': platform.platform(),
            'pid': os.getpid(), 'uid': os.getuid(), 'uptime_s': uptime,
            'load': os.getloadavg(), 'disk': {'total': disk.total, 'free': disk.free}, 'time': time.time()}


METHODS = {'status': status}
