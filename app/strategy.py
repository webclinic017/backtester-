import copy
import json
import logging
import os
from datetime import datetime
from decimal import Decimal

from app.exchange_client.base import BaseClient, OrderResult
from app.floating_steps import FloatingSteps
from app.models import Position, OnHoldPositions, Tick
from app.settings import app_settings
from app.telemetry.client import TelemetryClient

logger = logging.getLogger(__name__)


class BasicStrategy:
    tick_history_limit: int = 10

    def __init__(self, exchange_client: BaseClient, dry_run: bool = False) -> None:
        self._start_date: datetime = datetime.utcnow()
        self._open_positions: list[Position] = []
        self._closed_positions: list[Position] = []
        self._max_onhold_positions: OnHoldPositions | None = None
        self._max_sell_percent: Decimal = Decimal(0)
        self._max_sell_percent_tick: int = 0
        self._ticks_history: list[Tick] = []
        self._last_success_buy_tick_number: int = 0
        self._last_success_buy_price: Decimal = Decimal(0)

        self._exchange_client: BaseClient = exchange_client
        self._telemetry: TelemetryClient = TelemetryClient(
            filepath=os.path.join(app_settings.logs_path, app_settings.instance_name),
        )
        self._dry_run: bool = dry_run

    def has_tick_history(self) -> bool:
        return len(self._ticks_history) > 0

    def get_last_tick(self) -> Tick:
        return self._get_ticks_history()[-1]

    def get_previous_tick(self) -> Tick:
        return self._get_ticks_history()[-2]

    def tick(self, tick: Tick) -> bool:
        self._push_ticks_history(tick)
        self._update_stats(tick)
        buy_completed: bool = False

        if not app_settings.enabled:
            logger.warning('end trading session by enabled setting')
            return False

        if not tick.number:
            logger.info('init buy')
            self._telemetry.cleanup()

            for _ in range(app_settings.init_buy_amount):
                is_completed = self._open_position(
                    quantity=calculate_ticker_quantity(
                        app_settings.continue_buy_amount,
                        tick.ask,
                        app_settings.ticker_amount_digits,
                    ),
                    price=tick.ask,
                    tick_number=tick.number,
                )
                buy_completed = is_completed or buy_completed

            buy_price = None if not buy_completed else self._open_positions[-1].open_rate
            self._telemetry.push(
                tick.number,
                tick.avg_price,
                buy_price=buy_price,
            )

            self._update_stats(tick)
            return True

        # sale position[s]
        sale_completed = self._sell_something(bid_price=tick.bid, bid_qty=tick.bid_qty, tick_number=tick.number)

        # buy also
        if not app_settings.hold_position_limit or len(self._open_positions) < app_settings.hold_position_limit:
            logger.debug('try to buy something')
            buy_completed = self._buy_something(ask_price=tick.ask, ask_qty=tick.ask_qty, tick_number=tick.number)

        buy_price = None if not buy_completed else self._open_positions[-1].open_rate
        sell_price = None if not sale_completed else self._closed_positions[-1].close_rate
        self._telemetry.push(
            tick.number,
            tick.avg_price,
            buy_price=buy_price,
            sell_price=sell_price,
        )

        self._update_stats(tick)
        return True

    def show_results(self) -> None:
        results = self.get_results()

        print('')
        print('')
        print('Результаты тестирования:')
        print('')
        print('Общая оборотная сумма денег с начала запуска $%.2f / %.8f BTC (%.2f монет)' % (
            results['buy_total_amount_usd'],
            results['buy_total_amount_btc'],
            results['buy_total_qty'],
        ))

        print('')
        print('Оборотная сумма денег на покупки реализованных монет $%.2f / %.8f BTC (%.2f монет)' % (
            results['buy_without_current_opened_amount_usd'],
            results['buy_without_current_opened_amount_btc'],
            results['buy_without_current_opened_qty'],
        ))

        print('')
        print('Оборотная сумма денег за продажу реализованных монет $%.2f / %.8f BTC (%.2f монет)' % (
            results['sell_without_current_opened_amount_usd'],
            results['sell_without_current_opened_amount_btc'],
            results['sell_without_current_opened_qty'],
        ))
        print('Доходность без учёта зависших монет: $%.2f / %.8f BTC (%.2f%%)' % (
            results['dirty_pl_amount_usd'],
            results['dirty_pl_amount_btc'],
            results['dirty_pl_percent'],
        ))

        print('')
        print('Сумма денег за ликвидацию зависших монет $%.2f / %.8f BTC (%.2f монет)' % (
            results['liquidation_amount_usd'],
            results['liquidation_amount_btc'],
            results['liquidation_qty'],
        ))
        print('Доходность с учётом зависших монет: $%.2f / %.8f BTC (%.2f%%)' % (
            results['pl_amount_usd'],
            results['pl_amount_btc'],
            results['pl_percent'],
        ))

        print('')
        print('Требуемая сумма денег для обеспечения текущего тестирования $%.2f / %.8f BTC (%.1f монет, на тике %d)' % (
            results['onhold_amount_usd'],
            results['onhold_amount_btc'],
            results['onhold_qty'],
            results['onhold_tick_number'],
        ))

        print('')
        print('Количество покупок - %d' % results['count_buy_transactions'])
        print('Количество продаж - %d' % results['count_sell_transactions'])
        print('Количество не успешных сделок - %d' % results['count_unsuccessful_deals'])
        print('Количество успешных сделок - %d' % results['count_success_deals'])

    def save_results(self) -> None:
        logs_filepath = os.path.join(app_settings.logs_path, app_settings.instance_name)
        os.makedirs(logs_filepath, exist_ok=True)
        filepath = os.path.join(logs_filepath, 'result.json')

        with open(filepath, 'w') as fd:
            json.dump(self.get_results(), fd, default=str)

    def get_results(self) -> dict:
        buy_amount_without_current_opened = sum(
            [pos.open_rate * pos.amount for pos in self._closed_positions]
        )
        buy_amount_without_current_opened_fee = buy_amount_without_current_opened * app_settings.fee_percent / 100
        buy_without_current_opened = sum(
            [pos.amount for pos in self._closed_positions]
        )
        buy_amount_total = buy_amount_without_current_opened + sum(
            [pos.open_rate * pos.amount for pos in self._open_positions]
        )
        buy_amount_total_fee = buy_amount_total * app_settings.fee_percent / 100
        buy_total = buy_without_current_opened + sum(
            [pos.amount for pos in self._open_positions]
        )
        sell_amount_without_current_opened = sum(
            [pos.close_rate * pos.amount for pos in self._closed_positions]
        )
        sell_amount_without_current_opened_fee = sell_amount_without_current_opened * app_settings.fee_percent / 100
        sell_without_current_opened = sum(
            [pos.amount for pos in self._closed_positions]
        )
        liquidation_amount = sum(
            [self.get_last_tick().bid * pos.amount for pos in self._open_positions]
        )
        liquidation_amount_fee = liquidation_amount * app_settings.fee_percent / 100
        liquidation = sum(
            [pos.amount for pos in self._open_positions]
        )

        # считаем доходность относительно максимума средств в обороте
        max_amount_onhold: Decimal = self._max_onhold_positions.buy_amount if self._max_onhold_positions else Decimal(0)
        profit_amount_without_current_opened = sell_amount_without_current_opened - buy_amount_without_current_opened - sell_amount_without_current_opened_fee - buy_amount_without_current_opened_fee
        profit_amount_total = sell_amount_without_current_opened + liquidation_amount - buy_amount_total - sell_amount_without_current_opened_fee - liquidation_amount_fee - buy_amount_total_fee
        profit_percent_without_current_opened = (profit_amount_without_current_opened / max_amount_onhold * Decimal(
            100)) if max_amount_onhold else Decimal(0)
        profit_percent_total = (profit_amount_total / max_amount_onhold * Decimal(100)) if max_amount_onhold else Decimal(0)

        return {
            'start_date': self._start_date,

            'buy_total_amount_usd': (buy_amount_total + buy_amount_total_fee) * app_settings.symbol_to_usdt_rate,
            'buy_total_amount_btc': buy_amount_total + buy_amount_total_fee,
            'buy_total_qty': buy_total,

            'buy_without_current_opened_amount_usd': (buy_amount_without_current_opened + buy_amount_without_current_opened_fee) * app_settings.symbol_to_usdt_rate,
            'buy_without_current_opened_amount_btc': buy_amount_without_current_opened + buy_amount_without_current_opened_fee,
            'buy_without_current_opened_qty': buy_without_current_opened,

            'sell_without_current_opened_amount_usd': (sell_amount_without_current_opened - sell_amount_without_current_opened_fee) * app_settings.symbol_to_usdt_rate,
            'sell_without_current_opened_amount_btc': sell_amount_without_current_opened - sell_amount_without_current_opened_fee,
            'sell_without_current_opened_qty': sell_without_current_opened,

            'dirty_pl_amount_usd': profit_amount_without_current_opened * app_settings.symbol_to_usdt_rate,
            'dirty_pl_amount_btc': profit_amount_without_current_opened,
            'dirty_pl_percent': float(profit_percent_without_current_opened),

            'liquidation_amount_usd': (liquidation_amount - liquidation_amount_fee) * app_settings.symbol_to_usdt_rate,
            'liquidation_amount_btc': liquidation_amount - liquidation_amount_fee,
            'liquidation_qty': liquidation,

            'pl_amount_usd': profit_amount_total * app_settings.symbol_to_usdt_rate,
            'pl_amount_btc': profit_amount_total,
            'pl_percent': float(profit_percent_total),

            'onhold_amount_usd': self._max_onhold_positions.buy_amount * app_settings.symbol_to_usdt_rate if self._max_onhold_positions else 0,
            'onhold_amount_btc': self._max_onhold_positions.buy_amount if self._max_onhold_positions else 0,
            'onhold_qty': self._max_onhold_positions.quantity if self._max_onhold_positions else 0,
            'onhold_tick_number': self._max_onhold_positions.tick_number if self._max_onhold_positions else 0,

            'count_buy_transactions': len(self._closed_positions) + len(self._open_positions),
            'count_sell_transactions': len(self._closed_positions),
            'count_unsuccessful_deals': len(self._open_positions),
            'count_success_deals': len(self._closed_positions),
        }

    def _update_stats(self, tick: Tick):
        on_hold_current = OnHoldPositions(
            quantity=Decimal(sum([pos.amount for pos in self._open_positions])),
            buy_amount=Decimal(sum([pos.amount * pos.open_rate for pos in self._open_positions])),
            tick_number=tick.number,
            tick_rate=tick.avg_price,
        )
        if not self._max_onhold_positions or self._max_onhold_positions.buy_amount <= on_hold_current.buy_amount:
            self._max_onhold_positions = on_hold_current

    def _get_open_positions_for_sell(self) -> list[Position]:
        return sorted(copy.deepcopy(self._open_positions), key=lambda x: x.open_rate)

    def _open_position(self, quantity: Decimal, price: Decimal, tick_number: int) -> bool:
        if self._dry_run:
            buy_response: OrderResult | None = OrderResult(
                is_filled=True,
                qty=quantity,
                price=price,
                raw_response={'dry_run': True},
            )
        else:
            buy_response = self._exchange_client.buy(
                quantity=quantity,
                price=price,
            )

        logger.debug('open new position response {0}'.format(buy_response))
        if not buy_response or not buy_response.is_filled:
            logger.warning('open new position - unsuccessfully "{0}" {1}'.format(
                buy_response,
                {'quantity': quantity, 'price': price},
            ))
            return False

        logger.info('open new position {0} {1}'.format(buy_response.qty, buy_response.price))
        self._last_success_buy_tick_number = tick_number
        self._last_success_buy_price = buy_response.price
        self._open_positions.append(Position(
            amount=buy_response.qty,
            open_rate=buy_response.price,
            open_tick_number=tick_number,
        ))
        return True

    def _close_position(self, position_for_close: Position, price: Decimal, tick_number: int) -> bool:
        if self._dry_run:
            sell_response: OrderResult | None = OrderResult(
                is_filled=True,
                qty=position_for_close.amount,
                price=price,
                raw_response={'dry_run': True},
            )
        else:
            sell_response = self._exchange_client.sell(
                quantity=position_for_close.amount,
                price=price,
            )

        logger.debug('close position response {0}'.format(sell_response))
        if not sell_response or not sell_response.is_filled:
            logger.info('close position - unsuccessfully "{0}" {1}'.format(
                sell_response,
                {'quantity': position_for_close.amount, 'price': price},
            ))
            return False

        logger.info('close position')
        self._open_positions.remove(position_for_close)
        position_for_close.close_rate = sell_response.price
        position_for_close.close_tick_number = tick_number
        self._closed_positions.append(position_for_close)
        return True

    def _sell_something(self, bid_price: Decimal, bid_qty: Decimal, tick_number: int) -> bool:
        logger.debug('search position for sell. Tick price: {0}'.format(bid_price))

        qty_left: Decimal = bid_qty
        sale_completed: bool = False
        for position in self._get_open_positions_for_sell():
            logger.debug(position)

            # условия на продажу
            # - текущая цена выше цены покупки на 5%
            logger.debug('check sale by tick rate and open rate.')
            logger.debug('Position: {0}. Current price {1}. Open rate + 5%: {2}. Check {3}'.format(
                position,
                bid_price,
                position.open_rate * app_settings.avg_rate_sell_limit,
                bid_price >= position.open_rate * app_settings.avg_rate_sell_limit,
            ))

            sell_price = (bid_price * app_settings.sell_price_discount).quantize(app_settings.ticker_price_digits)
            if bid_price >= position.open_rate * app_settings.avg_rate_sell_limit and qty_left >= position.amount:
                sell_response = self._close_position(position, price=sell_price, tick_number=tick_number)
                sale_completed = sell_response or sale_completed

                if sell_response:
                    qty_left = qty_left - position.amount

            if sale_completed and not app_settings.multiple_sell_on_tick:
                break

        return sale_completed

    def _buy_something(self, ask_price: Decimal, ask_qty: Decimal, tick_number: int) -> bool:
        previous_price = self.get_previous_tick().ask
        if app_settings.use_last_open_position_rate and self._last_success_buy_price:
            previous_price = self._last_success_buy_price

        one_percent = previous_price / Decimal(100)
        rate_diff = previous_price - ask_price

        if app_settings.use_last_open_position_rate and self._last_success_buy_price:
            rate_diff = abs(rate_diff)

        rate_go_down_percent = rate_diff / one_percent
        buy_price = (ask_price * app_settings.buy_price_discount).quantize(app_settings.ticker_price_digits)
        buy_qty = calculate_ticker_quantity(
            app_settings.continue_buy_amount,
            buy_price,
            app_settings.ticker_amount_digits,
        )
        is_buy_available_by_frequency = (tick_number - self._last_success_buy_tick_number) >= app_settings.continue_buy_every_n_ticks
        is_buy_available_by_duplicate_rate = self._has_not_open_position_by_price(ask_price)
        is_buy_available_by_qty = (buy_qty <= ask_qty) or ask_qty == 0

        logger.debug('check rates for buy. Prev ask price: %.4f, diff %.4f, frequency buy lock %s, last buy tick %d, duplicate lock %s, qty check %s' % (
            float(previous_price),
            float(rate_go_down_percent),
            is_buy_available_by_frequency,
            self._last_success_buy_tick_number,
            is_buy_available_by_duplicate_rate,
            is_buy_available_by_qty,
        ))

        if rate_go_down_percent >= app_settings.step and is_buy_available_by_frequency and is_buy_available_by_duplicate_rate and is_buy_available_by_qty:
            return self._open_position(
                quantity=buy_qty,
                price=buy_price,
                tick_number=tick_number,
            )
        return False

    def _push_ticks_history(self, tick: Tick) -> None:
        if len(self._ticks_history) >= self.tick_history_limit:
            self._ticks_history.pop(0)
        self._ticks_history.append(tick)

    def _get_ticks_history(self) -> list[Tick]:
        return self._ticks_history

    def _has_not_open_position_by_price(self, price: Decimal) -> bool:
        test_rate = price.quantize(app_settings.ticker_price_digits)
        for position in self._open_positions:
            position_rate = position.open_rate.quantize(app_settings.ticker_price_digits)
            if position_rate == test_rate:
                return False
        return True


class FloatingStrategy(BasicStrategy):
    def __init__(self, exchange_client: BaseClient, steps_instance: FloatingSteps, dry_run: bool = False) -> None:
        super().__init__(exchange_client, dry_run)
        self._steps: FloatingSteps = steps_instance

    def _sell_something(self, bid_price: Decimal, bid_qty: Decimal, tick_number: int) -> bool:
        logger.debug('search position for sell. Tick price: {0}'.format(bid_price))

        qty_left: Decimal = bid_qty
        has_sale_try: bool = False
        sale_completed: bool = False

        for position in self._get_open_positions_for_sell():
            logger.debug(position)

            # условия на продажу
            # - текущая цена выше цены покупки на N%
            step_percent = self._steps.current_step / Decimal(100) + Decimal(1)
            logger.debug('check sale by tick rate and open rate.')
            logger.debug('Position: {0}. Current price {1}. Open rate +N% {2}. Check {3}. Percent {4}'.format(
                position,
                bid_price,
                position.open_rate * step_percent,
                bid_price >= position.open_rate * step_percent,
                step_percent,
            ))

            sell_price = (bid_price * app_settings.sell_price_discount).quantize(app_settings.ticker_price_digits)

            if qty_left >= position.amount:
                has_sale_try = True

                if bid_price >= position.open_rate * step_percent:
                    sell_response = self._close_position(position, price=sell_price, tick_number=tick_number)
                    sale_completed = sell_response or sale_completed
                    if sell_response:
                        qty_left = qty_left - position.amount

            if sale_completed and not app_settings.multiple_sell_on_tick:
                break

        if has_sale_try:
            if sale_completed:
                self._steps.to_next_step()
            else:
                self._steps.to_prev_step()

        return sale_completed

    def _update_stats(self, tick: Tick):
        super()._update_stats(tick)
        if self._steps.current_step >= self._max_sell_percent:
            self._max_sell_percent = self._steps.current_step
            self._max_sell_percent_tick = tick.number

    def get_results(self) -> dict:
        results = super().get_results()
        results['max_sell_percent'] = float(self._max_sell_percent)
        results['max_sell_tick'] = self._max_sell_percent_tick
        return results

    def show_results(self) -> None:
        super().show_results()

        results = self.get_results()
        print('')
        print('Максимальный %% торговли: %.2f%% на тике %d' % (
            results['max_sell_percent'],
            results['max_sell_tick'],
        ))


def calculate_ticker_quantity(needed_amount: Decimal, current_price: Decimal, round_digits: Decimal) -> Decimal:
    """Return ticker quantity by current price and needed amount in ticker currency (USDT for common cases)."""
    return (needed_amount / current_price).quantize(round_digits)


def get_strategy_instance(strategy_type: str, exchange_client: BaseClient, dry_run: bool) -> BasicStrategy:
    return {
        'basic': BasicStrategy(
            exchange_client=exchange_client,
            dry_run=dry_run,
        ),
        'floating': FloatingStrategy(
            exchange_client=exchange_client,
            steps_instance=FloatingSteps(app_settings.float_steps_path),
            dry_run=dry_run,
        ),
    }[strategy_type]
