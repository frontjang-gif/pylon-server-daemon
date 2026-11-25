"""Shared bounded compute runtime, executed on the endpoint."""
try:
    from daemons.workload_runtime import METHODS
except ImportError:
    from workload_runtime import METHODS
CAPABILITY='cap:system.workload'
VARIANT='compute'
ATTRS={'durable_receipts':True,'max_seconds':3600,'vfs_routes':['direct','proxy','proxy_stream'],'progress':True,'proxy_max_bytes':131072,'stream_max_bytes':16777216,'capacity_probe':True}
