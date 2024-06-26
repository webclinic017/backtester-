import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pyxirr
from pyxirr import xirr

from app.models import Position

logger = logging.getLogger(__file__)


def calculate_xirr(positions: list[Position], actual_rate: Decimal) -> Decimal:
    if not positions:
        return Decimal(0)

    cash_flow: list[tuple[datetime, Decimal]] = []
    open_position_qty = sum(
        position.amount
        for position in positions
        if not position.is_closed
    )
    if open_position_qty:
        # fake sold by actual price
        cash_flow.append(
            (datetime.utcnow(), open_position_qty * actual_rate * Decimal(-1)),
        )

    for position in positions:
        cash_flow.append(
            (position.open_tick_datetime, position.amount * position.open_rate),
        )
        if position.is_closed:
            cash_flow.append(
                (position.close_tick_datetime, position.amount * position.close_rate * Decimal(-1)),
            )

    cash_flow.sort(key=lambda x: x[0])
    try:
        res = xirr(
            dates=[v[0] for v in cash_flow],
            amounts=[v[1] for v in cash_flow],
        )
    except pyxirr.InvalidPaymentsError:
        logger.warning('XIRR exception {0} {1}'.format(
            open_position_qty,
            cash_flow,
        ))
        return Decimal(0)

    del cash_flow
    logger.info('XIRR raw value {0}'.format(res))
    if res is None:
        return Decimal(0)

    try:
        raw_xirr = (Decimal(res) * Decimal(100))
    except InvalidOperation:
        return Decimal(0)

    return min(Decimal(9999), raw_xirr).quantize(Decimal('0.0001'))
