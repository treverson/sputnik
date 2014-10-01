class RactiveSputnikWrapper
    constructor: (@ractive, @sputnik, @keypath, @prefix) ->
        @markets = {}
        @types = {}
        @books = {}
        @positions = {}
        @margin = [0, 0]
        @trade_history = {}

        @sputnik.on "markets", (markets) =>
            @markets = {}
            @types = {}

            for ticker, market of markets
                if market.contract_type isnt "cash"
                    @markets[ticker] = market
                    type = market.contract_type
                    (@types[type] or (@types[type] = [])).push ticker

            @notify "markets"
            @notify "types"

        @sputnik.on "book", (book) =>
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
            for ticker, history of trade_history
                @trade_history[ticker] = history.reverse()

            @notify "trade_history"

        sputnik.on "positions", (positions) =>
            for ticker, position of positions
                if @markets[ticker]?.contract_type isnt "cash_pair"
                    @positions[ticker] = position
            
            @notify "positions"
        
        sputnik.on "margin", (margin) =>
            @margin = margin
            
            @notify "margin"

    notify: (property) =>
        @setting = true
        @ractive.set @prefix property
        @setting = false

    get: () ->
        markets: @markets
        types: @types
        books: @books
        positions: @positions
        margin: @margin
        trade_history: @trade_history

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

