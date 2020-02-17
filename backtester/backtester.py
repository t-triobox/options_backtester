import numpy as np
import pandas as pd
import pyprind

from .enums import Order, Stock
from .datahandler import HistoricalOptionsData, TiingoData
from .strategy import Strategy


class Backtest:
    """Processes signals from the Strategy object"""
    def __init__(self, allocation, initial_capital=1_000_000, shares_per_contract=100):
        assert isinstance(allocation, dict)

        assets = ('stocks', 'options', 'cash')
        total_allocation = sum(allocation.get(a, 0.0) for a in assets)

        self.allocation = {}
        for asset in assets:
            self.allocation[asset] = allocation.get(asset, 0.0) / total_allocation

        self.current_cash = self.initial_capital = initial_capital
        self.stop_if_broke = True
        self.shares_per_contract = shares_per_contract
        self._stocks = []
        self._options_strategy = None
        self._stocks_data = None
        self._options_data = None

    @property
    def stocks(self):
        return self._stocks

    @stocks.setter
    def stocks(self, stocks):
        assert all(isinstance(stock, Stock) for stock in stocks), 'Invalid stocks'
        assert sum(stock.percentage for stock in stocks) == 1.0, 'Stock percentages must sum to 1.0'
        self._stocks = list(stocks)
        return self

    @property
    def strategy(self):
        return self._options_strategy

    @strategy.setter
    def strategy(self, strat):
        assert isinstance(strat, Strategy)
        self._options_strategy = strat
        self.current_cash = strat.initial_capital

    @property
    def stocks_data(self):
        return self._stocks_data

    @stocks_data.setter
    def stocks_data(self, data):
        assert isinstance(data, TiingoData)
        self._stocks_schema = data.schema
        self._stocks_data = data

    @property
    def options_data(self):
        return self._options_data

    @options_data.setter
    def options_data(self, data):
        assert isinstance(data, HistoricalOptionsData)
        self._options_schema = data.schema
        self._options_data = data

    def run(self, rebalance_freq=0, monthly=False):
        """Runs the backtest and returns a `pd.DataFrame` of the orders executed (`self.trade_log`)

        Args:
            rebalance_freq (int, optional): Determines the frequency of portfolio rebalances. Defaults to 0.
            monthly (bool, optional):       Iterates through data monthly rather than daily. Defaults to False.

        Returns:
            pd.DataFrame:                   Log of the trades executed.
        """

        assert self._stocks_data, 'Stock data not set'
        assert self._options_data, 'Options data not set'
        assert self._options_strategy, 'Options Strategy not set'
        assert self._options_data.schema == self._options_strategy.schema

        option_dates = self._options_data['date'].unique()
        stock_dates = self._stock_data['date'].unique()
        assert np.array_equal(stock_dates, option_dates), 'Stock and options dates do not match'

        self._initialize_inventories()
        self.trade_log = pd.DataFrame()
        self.balance = pd.DataFrame({
            'total_capital': self.current_cash,
            'total_cash': self.current_cash
        },
                                    index=[self.stock_data.start_date - pd.Timedelta(1, unit='day')])

        rebalancing_days = pd.date_range(
            self.stock_data.first_date, self.stock_data.end_date, freq=str(rebalance_freq) +
            'BMS') if rebalance_freq else []

        data_iterator = self._data_iterator(monthly)
        bar = pyprind.ProgBar(len(stock_dates), bar_char='█')

        for date, stocks, options in data_iterator:
            if date in rebalancing_days or date == self.stock_data.start_date:
                self._rebalance_portfolio(date, stocks, options)

            self._update_balance(date, stocks, options)
            bar.update()

        self.balance['% change'] = self.balance['total_capital'].pct_change()
        self.balance['accumulated return'] = (1.0 + self.balance['% change']).cumprod()

        return self.trade_log

    def _initialize_inventories(self):
        """Initialize empty stocks and options inventories."""
        columns = pd.MultiIndex.from_product(
            [[l.name for l in self._options_strategy.legs],
             ['contract', 'underlying', 'expiration', 'type', 'strike', 'cost', 'order']])
        totals = pd.MultiIndex.from_product([['totals'], ['cost', 'qty', 'date']])
        self._options_inventory = pd.DataFrame(columns=columns.append(totals))

        self._stocks_inventory = pd.DataFrame(columns=['symbol', 'price', 'qty'])

    def _data_iterator(self, monthly):
        """Returns combined iterator for stock and options data.
        Each step, it produces a tuple like the following:
            (date, stocks, options)

        Returns:
            generator: Daily/monthly iterator over `self.stock_data` and `self.options_data`.
        """
        if monthly:
            it = zip(self._stock_data.iter_months(), self._options_data.iter_months())
        else:
            it = zip(self._stock_data.iter_dates(), self._options_data.iter_dates())

        return ((date, stocks, options) for (date, stocks), (_, options) in it)

    def _rebalance_portfolio(self, date, stocks, options):
        """Rebalances the portfolio according to `self.allocation`."""

        stock_capital = self._current_stock_capital(stocks)
        options_capital = self._current_options_capital(options)
        total_capital = self.current_cash + stock_capital + options_capital
        options_allocation = self.allocation['options'] * total_capital
        stocks_allocation = self.allocation['stocks'] * total_capital

        # Clear inventories
        self._initialize_inventories()

        for stock in self._stocks:
            query = '{} == "{}"'.format(self.schema['symbol'], stock.symbol)
            stock_row = stocks.query(query)
            stock_price = stock_row[self._stocks_schema['adjClose']].values[0]
            qty = (stocks_allocation * stock.percentage) // stock_price
            stock_entry = pd.Series([stock.symbol, stock_price, qty], index=self._stocks_inventory.columns)
            self._stocks_inventory = self._stocks_inventory.append(stock_entry, ignore_index=True)

        self._sell_options()
        entry_signals = self._strategy.filter_entries(options, self.inventory, date)
        self._execute_entry(entry_signals)

        options_value = sum(self.options_inventory['totals']['cost'] * self.options_inventory['totals']['qty'])

        # Update current cash
        invested_capital = sum(self.inventory['cost'] * self.inventory['qty'])
        self.current_cash = money_total - invested_capital

    def _current_stock_capital(self, stocks):
        """Return the current value of the stocks inventory.

        Args:
            stocks (pd.DataFrame): Stocks data for the current time step.

        Returns:
            float: Total capital in stocks.
        """
        current_stocks = self._stocks_inventory.merge(stocks,
                                                      how='left',
                                                      left_on='symbol',
                                                      right_on=self._stock_schema['symbol'])
        return (current_stocks[self._stocks_schema['adjClose']] * current_stocks['qty']).sum()

    def _current_options_capital(self, options):
        total_cost = 0.0
        for leg in self._options_strategy.legs:
            current_options = self._options_inventory[leg.name].merge(options,
                                                                      how='left',
                                                                      left_on='contract',
                                                                      right_on=self._options_schema['contract'])
            price_col = ~(leg.direction).value
            total_cost += current_options[price_col].fillna(0.0).iloc[0] * current_options['qty']

        return total_cost

    def _execute_entry(self, entry_signals):
        """Executes entry orders and updates `self.inventory` and `self.trade_log`"""
        entry, total_price = self._process_entry_signals(entry_signals)

        if (not self.stop_if_broke) or (self.current_options_cash >= total_price):
            self.options_inventory = self.options_inventory.append(entry, ignore_index=True)
            self.trade_log = self.trade_log.append(entry, ignore_index=True)
            self.current_options_cash -= total_price

    def _execute_exit(self, exit_signals):
        """Executes exits and updates `self.inventory` and `self.trade_log`"""
        exits, exits_mask, total_costs = exit_signals

        self.trade_log = self.trade_log.append(exits, ignore_index=True)
        self.options_inventory.drop(self.options_inventory[exits_mask].index, inplace=True)
        self.current_options_cash -= sum(total_costs)

    def _process_entry_signals(self, entry_signals):
        """Returns the entry signals to execute and their cost."""

        if not entry_signals.empty:
            entry = entry_signals.iloc[0]
            return entry, entry['totals']['cost'] * entry['totals']['qty']
        else:
            return entry_signals, 0

    def _rebalance_portfolio(self, date, stocks, options):
        """Rebalance portfolio, done after an _update_balance"""

        #first we need to exit the options
        exit_signals = self._options_strategy.filter_exits(options, self.options_inventory, date)
        self._execute_exit(exit_signals)

        leg_candidates = [
            self._options_strategy._exit_candidates(l.direction, self.options_inventory[l.name], options,
                                                    self.options_inventory.index) for l in self._options_strategy.legs
        ]

        # If a contract is missing we replace the NaN values with those of the inventory
        # except for cost, which we imput as zero.

        for leg in leg_candidates:
            leg['cost'].fillna(0, inplace=True)

        calls_value = -np.sum(
            sum(leg['cost'] * self.options_inventory['totals']['qty']
                for leg in leg_candidates if (leg['type'] == 'call').any()))
        puts_value = -np.sum(
            sum(leg['cost'] * self.options_inventory['totals']['qty']
                for leg in leg_candidates if (leg['type'] == 'put').any()))

        options_capital = calls_value + puts_value
        old_options_capital = self.current_options_cash + options_capital

        costs = []
        for stock in self.stocks:
            query = '{} == "{}"'.format(self.stock_data.schema['symbol'], stock.symbol)
            current_stock = stocks.query(query)
            current_stock_price = current_stock[self.stock_data.schema['adjClose']].values[0]
            stock_inventory = self.stocks_inventory.query(query)
            if stock_inventory.empty:
                qty = 0
            else:
                qty = stock_inventory['qty'].values[0]

            costs.append(current_stock_price * qty)

        old_stock_capital = self.current_stocks_cash + sum(costs)
        if old_options_capital + old_stock_capital != 0:

            self.total_capital = old_options_capital + old_stock_capital

        new_stocks_capital = self.total_capital * self.stocks_percentaje

        new_options_capital = self.total_capital * self.options_percentaje

        #update stock with new_stock_capital
        stocks_costs = []
        for stock in self.stocks:
            query = '{} == "{}"'.format(self.stock_data.schema['symbol'], stock.symbol)
            current_stock = stocks.query(query)
            current_stock_price = current_stock[self.stock_data.schema['adjClose']].values[0]
            qty = (new_stocks_capital * stock.percentage) // current_stock_price
            stocks_costs.append(qty * current_stock_price)
            stocks_inventory_entry = self.stocks_inventory.query(query)
            self.stocks_inventory.drop(stocks_inventory_entry.index, inplace=True)
            updated_asset = pd.Series([stock.symbol, current_stock_price, qty])
            updated_asset.index = self.stocks_inventory.columns
            self.stocks_inventory = self.stocks_inventory.append(updated_asset, ignore_index=True)

        self.stock_capital = new_stocks_capital
        self.current_stocks_cash = self.stock_capital - sum(stocks_costs)
        #update options

        self.current_options_cash += new_options_capital - self.current_options_cash
        self._options_strategy.initial_capital = new_options_capital
        entry_signals = self._options_strategy.filter_entries(options, self.options_inventory, date)
        self._execute_entry(entry_signals)
        self.options_capital = new_options_capital

    def _update_balance(self, date, stocks, options):
        """Updates positions and calculates statistics for the current date.

        Args:
            date (pd.Timestamp):    Current date.
            stocks (pd.DataFrame):  DataFrame of stocks
            options (pd.DataFrame): DataFrame of (daily/monthly) options.
        """
        exit_signals = self._options_strategy.filter_exits(options, self.options_inventory, date)
        self._execute_exit(exit_signals)

        # update options
        leg_candidates = [
            self._options_strategy._exit_candidates(l.direction, self.options_inventory[l.name], options,
                                                    self.options_inventory.index) for l in self._options_strategy.legs
        ]

        # If a contract is missing we replace the NaN values with those of the inventory
        # except for cost, which we imput as zero.

        for leg in leg_candidates:
            leg['cost'].fillna(0, inplace=True)

        calls_value = -np.sum(
            sum(leg['cost'] * self.options_inventory['totals']['qty']
                for leg in leg_candidates if (leg['type'] == 'call').any()))
        puts_value = -np.sum(
            sum(leg['cost'] * self.options_inventory['totals']['qty']
                for leg in leg_candidates if (leg['type'] == 'put').any()))

        options_capital = calls_value + puts_value
        self.options_capital = self.current_options_cash + options_capital
        #   if self.balance ==
        #update stocks portfolio information due to change in price over time
        costs = []
        for stock in self.stocks:
            query = '{} == "{}"'.format(self.stock_data.schema['symbol'], stock.symbol)
            asset_current = stocks.query(query)
            cost = asset_current[self.stock_data.schema['adjClose']].values[0]
            stock_inventory = self.stocks_inventory.query(query)
            qty = qty = stock_inventory['qty'].values[0]

            costs.append(cost * qty)
        total_value = sum(costs)

        self.stock_capital = total_value + self.current_stocks_cash
        self.total_capital = self.stock_capital + self.options_capital

        row = pd.Series(
            {
                'total capital': self.stock_capital + self.options_capital,
                'cash': self.current_stocks_cash + self.current_options_cash,
                'stocks capital': self.stock_capital,
                'stocks qty': self._stocks_inventory['qty'].sum(),
                'options capital': options_capital,
                'options qty': self._options_inventory['totals']['qty'].sum(),
                'calls capital': calls_capital,
                'puts capital': puts_capital
            },
            name=date)
        self.balance = self.balance.append(row)

    def summary(self):
        """Returns a table with summary statistics about the trade log"""
        df = self.trade_log
        balance = self.balance
        df.loc[:,
               ('totals',
                'capital')] = (-df['totals']['cost'] * df['totals']['qty']).cumsum() + self._strategy.initial_capital

        daily_returns = balance['% change'] * 100

        first_leg = self._strategy.legs[0].name

        entry_mask = df[first_leg].eval('(order == @Order.BTO) | (order == @Order.STO)')
        entries = df.loc[entry_mask]
        exits = df.loc[~entry_mask]

        costs = np.array([])
        for contract in entries[first_leg]['contract']:
            entry = entries.loc[entries[first_leg]['contract'] == contract]
            exit_ = exits.loc[exits[first_leg]['contract'] == contract]
            try:
                # Here we assume we are entering only once per contract (i.e both entry and exit_ have only one row)
                costs = np.append(costs, (entry['totals']['cost'] * entry['totals']['qty']).values[0] +
                                  (exit_['totals']['cost'] * exit_['totals']['qty']).values[0])
            except IndexError:
                continue

        # trades = entries.merge(exits,
        #                        on=[(l.name, 'contract') for l in self._strategy.legs],
        #                        suffixes=['_entry', '_exit'])

        # costs = trades.apply(lambda row: row['totals_entry']['cost'] + row['totals_exit']['cost'], axis=1)

        wins = costs < 0
        losses = costs >= 0
        profit_factor = np.sum(wins) / np.sum(losses)
        total_trades = len(exits)
        win_number = np.sum(wins)
        loss_number = total_trades - win_number
        win_pct = (win_number / total_trades) * 100
        largest_loss = max(0, np.max(costs))
        avg_profit = np.mean(-costs)
        avg_pl = np.mean(daily_returns)
        total_pl = (df['totals']['capital'].iloc[-1] / self._strategy.initial_capital) * 100

        data = [
            total_trades, win_number, loss_number, win_pct, largest_loss, profit_factor, avg_profit, avg_pl, total_pl
        ]
        stats = [
            'Total trades', 'Number of wins', 'Number of losses', 'Win %', 'Largest loss', 'Profit factor',
            'Average profit', 'Average P&L %', 'Total P&L %'
        ]
        strat = ['Strategy']
        summary = pd.DataFrame(data, stats, strat)

        # Applies formatters to rows
        def format_row_wise(styler, formatters):
            for row, row_formatter in formatters.items():
                row_num = styler.index.get_loc(row)

                for col_num in range(len(styler.columns)):
                    styler._display_funcs[(row_num, col_num)] = row_formatter

            return styler

        formatters = {
            "Total trades": lambda x: f"{x:.0f}",
            "Number of wins": lambda x: f"{x:.0f}",
            "Number of losses": lambda x: f"{x:.0f}",
            "Win %": lambda x: f"{x:.2f}%",
            "Largest loss": lambda x: f"${x:.2f}",
            "Profit factor": lambda x: f"{x:.2f}",
            "Average profit": lambda x: f"${x:.2f}",
            "Average P&L %": lambda x: f"{x:.2f}%",
            "Total P&L %": lambda x: f"{x:.2f}%"
        }

        styler = format_row_wise(summary.style, formatters)

        return styler

    def __repr__(self):
        return "Backtest(capital={}, allocation={}, stocks={}, strategy={})".format(
            self.current_cash, self.allocation, self._stocks, self._options_strategy)
