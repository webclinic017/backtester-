import logging
from decimal import Decimal

from app.exchange_client.base import BaseClient
from app.models import Position, OnHoldPositions, Tick
from app.settings import app_settings

logger = logging.getLogger(__name__)


class Strategy:
    tick_history_limit: int = 10

    def __init__(self, exchange_client: BaseClient | None = None) -> None:
        self.open_positions: list[Position] = []
        self.closed_positions: list[Position] = []
        self.max_onhold_positions: OnHoldPositions | None = None
        self._ticks_history: list[Tick] = []
        self._exchange_client = exchange_client

    def get_ticks_history(self) -> list[Tick]:
        return self._ticks_history

    def tick(self, tick: Tick) -> bool:
        self._push_ticks_history(tick)
        self._update_max_hold_positions(tick)

        if not tick.number:
            logger.info('init buy')
            for _ in range(app_settings.init_buy_amount):
                self._open_position(
                    quantity=app_settings.continue_buy_amount,
                    price=float(tick.price),
                    tick_number=tick.number,
                )
            return True

        if tick.number == 1:
            logger.info('skip')
            return True

        if tick.number >= app_settings.ticks_amount_limit:
            logger.warning('end trading session by tick limit')
            return False

        # check global stop loss
        if tick.price <= app_settings.global_stop_loss:
            logger.warning('global stop loss fired! open: {0}. closed: {1}'.format(
                len(self.open_positions),
                len(self.closed_positions),
            ))
            while len(self.open_positions):
                self._close_position(open_position_index=0, price=float(tick.price), tick_number=tick.number)
            self._update_max_hold_positions(tick)
            return False

        # search position for sale
        sale_completed = self._sell_something(price=float(tick.price), tick_number=tick.number)

        # вот смотри там где разница была больше или равно 0.02 мы закупали
        if sale_completed:
            logger.info('sale something')

        else:
            logger.info('buy something')
            self._buy_something(price=tick.price, tick_number=tick.number)

        self._update_max_hold_positions(tick)
        return True

    def show_results(self) -> None:
        # считаем доходность
        buy_amount = sum(
            [pos.open_rate * pos.amount for pos in self.closed_positions]
        ) + sum(
            [pos.open_rate * pos.amount for pos in self.open_positions]
        )
        sell_amount = sum(
            [pos.close_rate * pos.amount for pos in self.closed_positions]
        )
        hold_amount = sum(
            [float(self.get_ticks_history()[-1].price) * pos.amount for pos in self.open_positions]
        )

        print('')
        print('Результаты тестирования:')
        print(f'открытых позиций на конец торгов {len(self.open_positions)}')
        print(f'закрытых позиций на конец торгов {len(self.closed_positions)}')
        print('')
        print('потратили на покупки монет $%.2f' % buy_amount)
        print('получили монет с продажи монет $%.2f' % sell_amount)
        print('сумма за ликвидацию зависших монет $%.2f' % hold_amount)
        print('')

        buy_amount = buy_amount or 1.0
        print('доходность без учёта зависших монет: %.2f%%' % ((sell_amount - buy_amount) / buy_amount * 100))
        print('доходность без учёта зависших монет: $%.2f' % (sell_amount - buy_amount))
        print('')
        print('доходность с учётом зависших монет: %.2f%%' % (
                    (sell_amount + hold_amount - buy_amount) / buy_amount * 100))
        print('доходность с учётом зависших монет: $%.2f' % (sell_amount + hold_amount - buy_amount))

        if self.max_onhold_positions:
            print('')
            print('максимум %.2f монет на руках на тике %d ($%.2f по курсу на этот тик, цена открытия $%.2f)' % (
                self.max_onhold_positions.quantity,
                self.max_onhold_positions.tick_number,
                self.max_onhold_positions.tick_rate * self.max_onhold_positions.quantity,
                self.max_onhold_positions.buy_amount,
            ))

    def _update_max_hold_positions(self, tick: Tick):
        on_hold_current = OnHoldPositions(
            quantity=sum([pos.amount for pos in self.open_positions]),
            buy_amount=sum([pos.amount * pos.open_rate for pos in self.open_positions]),
            tick_number=tick.number,
            tick_rate=float(tick.price),
        )
        if not self.max_onhold_positions or self.max_onhold_positions.quantity < on_hold_current.quantity:
            self.max_onhold_positions = on_hold_current

    def _open_position(self, quantity: float, price: float, tick_number: int) -> bool:
        # todo use self._exchange_client here
        self.open_positions.append(Position(
            amount=quantity,
            open_rate=price,
            open_tick_number=tick_number,
        ))
        return True

    def _close_position(self, open_position_index: int, price: float, tick_number: int) -> bool:
        # todo use self._exchange_client here
        position_for_close = self.open_positions.pop(open_position_index)
        position_for_close.close_rate = price
        position_for_close.close_tick_number = tick_number
        self.closed_positions.append(position_for_close)
        return True

    def _sell_something(self, price: float, tick_number: int) -> bool:
        avg_rate: Decimal = self._get_history_average_price()
        logger.debug('search position for sell. Avg rate: {0}, tick price: {1}'.format(avg_rate, price))

        sale_completed: bool = False
        for open_position_index, position in enumerate(self.open_positions):
            logger.debug(position)

            # условия на продажу
            # - средняя превысила цену покупки на 5%
            logger.debug('check sale by avg rate and open rate. Open rate +5% {0}. Average rate +5% {1}'.format(
                position.open_rate * app_settings.avg_rate_sell_limit,
                float(avg_rate) * app_settings.avg_rate_sell_limit,
            ))
            if float(avg_rate) >= position.open_rate * app_settings.avg_rate_sell_limit:
                # - текущая цена выше чем средняя +5%
                if price >= float(avg_rate) * app_settings.avg_rate_sell_limit:
                    self._close_position(open_position_index=open_position_index, price=price, tick_number=tick_number)
                    logger.info('close position {0}'.format(open_position_index))
                    sale_completed = True
                    break

            # - текущая цена выше цены покупки на 0.02+5%
            logger.debug('check sale by tick rate and open rate. Open rate + step + 5%: {0}'.format(
                position.open_rate * app_settings.avg_rate_sell_limit + app_settings.step,
            ))
            if price >= position.open_rate * app_settings.avg_rate_sell_limit + app_settings.step:
                self._close_position(open_position_index=open_position_index, price=price, tick_number=tick_number)
                logger.info('close position {0}'.format(open_position_index))
                sale_completed = True
                break

        return sale_completed

    def _buy_something(self, price: Decimal, tick_number: int) -> None:
        rate_go_down = self.get_ticks_history()[-2].price - price
        logger.debug('check rates for buy. Prev rate: {0}, diff {1}'.format(
            self.get_ticks_history()[-2],
            rate_go_down,
        ))
        if rate_go_down >= app_settings.step:
            logger.debug('open new position')
            self._open_position(
                quantity=app_settings.continue_buy_amount,
                price=float(price),
                tick_number=tick_number,
            )

    def _push_ticks_history(self, tick: Tick) -> None:
        if len(self._ticks_history) >= self.tick_history_limit:
            self._ticks_history.pop(0)
        self._ticks_history.append(tick)

    def _get_history_average_price(self) -> Decimal:
        return (self.get_ticks_history()[-3].price + self.get_ticks_history()[-2].price) / 2