"""Phase-2 building block, NOT wired to phase-1 REST snapshots.

Binance official diff-depth U/u continuity. The caller must buffer WebSocket
messages before requesting the REST snapshot, replay the buffer and resnapshot
on SequenceGap. Never use an invalidated book for evidence scoring.
"""
from decimal import Decimal
from lfm32_sources import decimal


class SequenceGap(ValueError):
    pass


class BinanceBook:
    def __init__(self, snapshot):
        self.sequence = int(snapshot['lastUpdateId'])
        self.bids = {decimal(p, True): decimal(q) for p,q in snapshot['bids']}
        self.asks = {decimal(p, True): decimal(q) for p,q in snapshot['asks']}
        self.valid = False
        self.failed = False

    def apply(self, event):
        if self.failed:
            raise SequenceGap('book invalidated; fetch a new snapshot and replay buffered events')
        first, last = int(event['U']), int(event['u'])
        if event.get('s', 'BTCUSDT') != 'BTCUSDT' or first > last:
            self.failed = True; self.valid = False
            raise SequenceGap('invalid symbol or update range')
        if last <= self.sequence:
            return False
        if not first <= self.sequence+1 <= last:
            self.failed = True; self.valid = False
            raise SequenceGap('missing diff-depth update; resnapshot required')
        # Validate complete event before changing either side.
        try:
            updates = {side: [(decimal(p,True), decimal(q)) for p,q in event[key]] for side,key in [('bids','b'),('asks','a')]}
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            self.failed = True; self.valid = False
            raise SequenceGap('malformed update; resnapshot required') from error
        for side, levels in updates.items():
            book = getattr(self, side)
            for price, size in levels:
                if size == Decimal(0): book.pop(price, None)
                else: book[price] = size
        self.sequence = last
        self.valid = True
        return True
