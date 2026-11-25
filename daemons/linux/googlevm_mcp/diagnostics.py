"""Summarize recorded HTTP exchanges without reading their raw body files."""
from datetime import datetime


def summarize(events, tool_calls=0):
    grouped = {}
    for event in events:
        payload = event['payload']
        tid = payload.get('traffic_id')
        if not tid: continue
        item = grouped.setdefault(tid, {'traffic_id':tid,'rpc_method':payload.get('rpc_method'),
            'rpc_id':payload.get('rpc_id'),'status':None,'request_bytes':0,'response_bytes':0,
            'complete':False,'started':event['ts'],'finished':event['ts'],'duration_ms':None})
        item['finished']=event['ts']
        if event['kind']=='protocol.request':
            item['request_bytes']=payload.get('body_bytes',0)
            item['started']=event['ts']
        if payload.get('status') is not None: item['status']=payload['status']
        if event['kind']=='protocol.response':
            item['response_bytes']=payload.get('body_bytes',0)
            item['complete']=payload.get('complete',False)
        if event['kind'] in ('protocol.incomplete','protocol.error'): item['complete']=False
        if 'elapsed_ms' in payload: item['duration_ms']=payload['elapsed_ms']
    items=list(grouped.values())
    latest_initialization=max((item['started'] for item in items if item['rpc_method']=='initialize' and item['status']==200),default='')
    for item in items:
        if item['duration_ms'] is None:
            item['duration_ms']=round(max(0,(datetime.fromisoformat(item['finished'])-datetime.fromisoformat(item['started'])).total_seconds()*1000),2)
        later_initialization=latest_initialization>=item['started']
        status=item['status'] or 0
        item['outcome']=('Negotiated fallback' if item['rpc_method']=='server/discover' and status==400 and later_initialization else
            'Authentication challenge' if status==401 else 'HTTP error' if status>=400 else
            'Complete' if item['complete'] else 'Incomplete')
    return {'requests':len(items),'tool_calls':tool_calls,
        'errors':sum(item['outcome']=='HTTP error' for item in items),
        'incomplete':sum(item['outcome']=='Incomplete' for item in items),
        'negotiation_fallbacks':sum(item['outcome']=='Negotiated fallback' for item in items),
        'authentication_challenges':sum(item['outcome']=='Authentication challenge' for item in items),
        'initialize_requests':sum(item['rpc_method']=='initialize' for item in items),
        'request_bytes':sum(item['request_bytes'] for item in items),
        'response_bytes':sum(item['response_bytes'] for item in items),
        'max_duration_ms':max((item['duration_ms'] for item in items),default=0),'exchanges':items}
