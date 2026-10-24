def make_sma_strategy(window):
    """
    简单移动平均线趋势策略（只做多）。

    规则：今天收盘价【严格高于】最近 window 根收盘价的平均值 -> 持有(1)，否则空仓(0)。
    数据不足 window 根时（预热期）一律空仓(0)。

    返回的函数符合 build_signals 的要求：
    strategy(opens_so_far, closes_so_far) -> 0 或 1，
    只使用截至今天收盘的数据，不会用到未来数据。
    """
    if type(window) is not int or window < 2:
        raise ValueError("均线窗口必须是不小于 2 的整数。")

    def strategy(opens_so_far, closes_so_far):
        if len(closes_so_far) < window:
            return 0
        recent = closes_so_far[-window:]
        average = sum(recent) / window
        return 1 if closes_so_far[-1] > average else 0

    return strategy

