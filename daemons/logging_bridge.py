"""Bounded, connection-scoped Python logging relay over the existing agent socket."""
import asyncio
import json
import logging
import queue


class Relay(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = queue.Queue(maxsize=256)
        self.dropped = 0

    def emit(self, record):
        if record.levelno < logging.WARNING and record.name != 'system/wakeup':
            return
        try:
            self.records.put_nowait({'type': 'log', 'level': record.levelname.lower(),
                'source': record.name, 'message': self.format(record)[:16000],
                'fields': getattr(record, 'fields', {})})
        except queue.Full:
            self.dropped += 1
        except Exception:
            pass

    async def run(self, ws):
        root = logging.getLogger()
        root.addHandler(self)
        try:
            await ws.send(json.dumps({'type': 'log', 'level': 'info', 'source': 'daemon', 'message': 'Structured log relay connected'}))
            while True:
                for _ in range(32):
                    try:
                        record = self.records.get_nowait()
                    except queue.Empty:
                        break
                    if self.dropped:
                        record.setdefault('fields', {})['relay_dropped'] = self.dropped
                    await ws.send(json.dumps(record))
                await asyncio.sleep(.2)
        finally:
            root.removeHandler(self)
            self.close()
