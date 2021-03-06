#!/usr/bin/env python
#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

class RactiveSputnikWrapper
    constructor: (@ractive, @sputnik, @keypath, @prefix) ->
        @logged_in = false
        @username = null
        @markets = {}
        @types = {}
        @books = {}
        @positions = {}
        @margin = [0, 0]
        @trade_history = {}
        @orders = []
        @ohlcv = {}
        @currencies = {}
        @transaction_history = {}
        @audit = {}
        @audit_hash = ''
        @exchange_info = {}
        @active_contracts = []
        @profile =
            locale: navigator.language
        @cash_spent = {}
        @position_contracts = {}
        @safe_prices = {}
        @permissions = {}
        @api = {}

        @sputnik.on "cash_spent", (cash_spent) =>
            @cash_spent = cash_spent
            @notify "cash_spent"

        @sputnik.on "audit_details", (audit_details) =>
            @audit = audit_details
            @notify "audit"

        @sputnik.on "exchange_info", (exchange_info) =>
            @exchange_info = exchange_info
            if @exchange_info.supported_ids?
                @exchange_info.supported_ids = [id_type.trim() for id_type in @exchange_info.supported_ids.split(",")]
            else
                @exchange_info.supported_ids = ['passport', 'drivers_license', 'personal_identification']

            if @exchange_info.restrict_full_ui? and @exchange_info.restrict_full_ui.toLowerCase() == "true"
                @exchange_info.restrict_full_ui = true
            else
                @exchange_info.restrict_full_ui = false

            @notify "exchange_info"

            if @exchange_info.locale?
                @profile.locale = @exchange_info.locale
                @notify "profile"

        @sputnik.on "permissions", (permissions) =>
            @permissions = permissions
            @notify "permissions"

        @sputnik.on "audit_hash", (audit_hash) =>
            @audit_hash = audit_hash
            @notify "audit_hash"

        @sputnik.on "transaction_history", (history) =>
            @transaction_history = {}
            for item in history
                if @transaction_history[item.contract]?
                    @transaction_history[item.contract].push item
                else
                    @transaction_history[item.contract] = [item]

            @notify "transaction_history"

        @sputnik.on "auth_success", (username) =>
            @logged_in = true
            @username = username
            console.log "logged in as #{username}"

            @notify "username"
            @notify "logged_in"

        @sputnik.on "profile", (profile) =>
            @profile = profile
            @notify "profile"

        @sputnik.on "markets", (markets) =>
            @sputnik.log ["markets", markets]
            @markets = {}
            @types = {}

            now = new Date().getTime()
            @position_contracts = {}

            for ticker, market of markets
                if market.expiration/1000 < now
                    market.expired = true
                else
                    market.expired = false

                if market.contract_type isnt "cash"
                    @markets[ticker] = market
                    type = market.contract_type
                    (@types[type] or (@types[type] = [])).push ticker
                    # Later we'll come up with a better rule for what is an active contract
                    # but for now if we're not logged in, it is all contracts you can trade
                    @active_contracts.push ticker
                else
                    @currencies[ticker] = market

                if market.contract_type isnt "cash_pair"
                    # All contracts you can have a position in
                    @position_contracts[ticker] = market

                    if not @positions[ticker]?
                        @positions[ticker] =
                            position: 0

            @notify "markets"
            @notify "types"
            @notify "currencies"
            @notify "active_contracts"
            @notify "position_contracts"
            @notify "positions"

        @sputnik.on "address", (address) =>
            @currencies[address[0]].address = address[1]
            @notify "currencies"

        @sputnik.on "deposit_instructions", (instructions) =>
            @currencies[instructions[0]].instructions = instructions[1]
            @notify "currencies"

        @sputnik.on "book", (book) =>
            @sputnik.log ["book", book]
            ticker = book.contract

            @books[ticker] =
                bids: book.bids
                asks: book.asks
                best_ask:
                    price: Infinity
                    quantity: 0
                best_bid:
                    price: 0
                    quantity: 0

            if book.asks.length
                @books[ticker].best_ask = book.asks[0]
            if book.bids.length
                @books[ticker].best_bid = book.bids[0]

            @notify "books"

        @sputnik.on "trade_history", (trade_history) =>
            @sputnik.log ["trade_history", trade_history]
            for ticker, history of trade_history
                @trade_history[ticker] = history.reverse()

            @notify "trade_history"

        @sputnik.on "positions", (positions) =>
            @sputnik.log ["positions", positions]
            for ticker, position of positions
                if @markets[ticker]?.contract_type isnt "cash_pair"
                    @positions[ticker] = position

            sputnik.log ["positions", @positions]
            @notify "positions"

            # Set active contracts based on what we have positions in
            tmp_active_contracts = []
            for ticker, market of @markets
                if market.contract_type is "cash_pair"
                    if ticker not in tmp_active_contracts
                        if @positions[market.denominated_contract_ticker]? and @positions[market.denominated_contract_ticker].position != 0
                            tmp_active_contracts.push ticker
                        else if @positions[market.payout_contract_ticker]? and @positions[market.payout_contract_ticker].position != 0
                            tmp_active_contracts.push ticker

                else if market.contract_type isnt "cash"
                    if ticker not in tmp_active_contracts
                        if @positions[ticker]?.position
                            tmp_active_contracts.push ticker

            # If there's nothing that results, don't change
            if tmp_active_contracts.length
                @active_contracts = tmp_active_contracts
                @sputnik.log ["active_contracts", @active_contracts]
                @notify "active_contracts"
        
        @sputnik.on "margin", (margin) =>
            @sputnik.log ["margin", margin]
            @margin = margin

            @notify "margin"

        @sputnik.on "orders", (orders) =>
            @sputnik.log ["orders", orders]
            @orders = orders

            @notify "orders"

        @sputnik.on "ohlcv_history", (ohlcv_history) =>
            @sputnik.log ["ohlcv_history", ohlcv_history]
            keys = Object.keys(ohlcv_history).sort()
            if keys.length
                last_key = keys[keys.length-1]
                ohlcv = ohlcv_history[last_key]
                update_ohlcv(ohlcv)

        update_ohlcv = (ohlcv) =>
            if ohlcv.contract of @ohlcv
                @ohlcv[ohlcv.contract][ohlcv.period] = ohlcv
            else
                @ohlcv[ohlcv.contract] = {}
                @ohlcv[ohlcv.contract][ohlcv.period] = ohlcv

            @notify "ohlcv"

        @sputnik.on "ohlcv", (ohlcv) =>
            @sputnik.log ["ohlcv", ohlcv]
            update_ohlcv(ohlcv)

        @sputnik.on "safe_prices", (@safe_prices) =>
            @sputnik.log ["safe_prices", @safe_prices]
            @notify "safe_prices"

        @sputnik.on "api", (@api) =>
            @notify "api"

    notify: (property) =>
        @setting = true
        @ractive.set @prefix property
        @setting = false

    get: () ->
        logged_in: @logged_in
        username: @username
        markets: @markets
        types: @types
        books: @books
        positions: @positions
        margin: @margin
        trade_history: @trade_history
        orders: @orders
        ohlcv: @ohlcv
        currencies: @currencies
        transaction_history: @transaction_history
        audit: @audit
        audit_hash: @audit_hash
        exchange_info: @exchange_info
        active_contracts: @active_contracts
        profile: @profile
        cash_spent: @cash_spent
        position_contracts: @position_contracts
        safe_prices: @safe_prices
        permissions: @permissions
        api: @api

    set: (property, value) =>
        # this is called both, when we update, and when the user updates
        # we do not want infinite loops, so check
        # check for internal event

        if @setting
            return

    reset: (data) =>
        return false

    teardown: () =>
        delete @sputnik

Ractive.adaptors.Sputnik =
    filter: (object) ->
        object instanceof Sputnik

    wrap: (ractive, sputnik, keypath, prefix) ->
        new RactiveSputnikWrapper(ractive, sputnik, keypath, prefix)

