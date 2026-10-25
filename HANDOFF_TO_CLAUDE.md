> 2026-10-25 注：这是当时的交接，不是当前现状。下文「不要直接发送订单」已被后续模拟盘记录超过。后续以仓库代码、PROJECT_STATUS.md 和 EXPERIMENTS.md 为准。正文不改。

AI Crypto Trader — 阶段 2c 交接报告（GPT → Claude）

日期： 2026-10-25
任务： OKX 模拟盘下单最小实测，仅现货 BTC-USDT
当前结论： 暂不具备进入真实模拟盘下单实测的充分条件。先审查工作区当前代码、修复订单状态与停机恢复问题，再进行全假网络测试。不要直接发送订单。

一、已确认的实际进展

1. Pyto 环境与数据验证

* 项目目录：/Documents/AI_Crypto_Trader
* Pyto Python 版本：3.14.0rc3
* 工作区读写 smoke test：PASS
* 数据验证器合成测试：8/8 PASS
    * duplicate_timestamp
    * incomplete_candle
    * invalid_ohlc
    * missing_daily_candle
    * negative_volume
    * valid_data
    * wrong_header
    * wrong_order

2. OKX 只读访问

用户曾实际运行只读模拟盘账户查询，结果为：

HTTP 状态码：200
code：0
msg：
接口层结果：成功
USDT 可用余额：4410.353540433386
只读账户查询：PASS
USDT 可用余额大于 0：PASS

BTC-USDT 现货 instruments 查询曾返回：

minSz: 0.00002
lotSz: 0.00000001
tickSz: 0.1

以上结果仅证明当时的只读请求成功，不代表已验证下单权限、下单接口或全部地区合规资格。余额和交易规则会变化，真实运行时必须重新读取。

3. 已报告通过的测试

文件	用户报告的结果
test_order_safety.py	18 项通过
test_order_manager.py	39 项通过
test_order_store.py	6 项通过
数据验证器合成测试	8 项通过

注意：以上是修改前或相应历史运行时用户报告的结果。GPT 后续修改了 order_manager.py 的四处代码，修改后的 39 项测试是否仍通过，尚未验证。不要将历史结果误报为当前工作区测试结果。

test_okx_demo.py 的完整通过数量没有可靠记录，不要推测。

二、GPT 审查过的文件

用户已经提供过完整代码，GPT 已审查：

* order_manager.py
* order_store.py
* exchange_base.py
* okx_demo.py
* fake_exchange.py
* test_order_manager.py
* test_okx_demo.py

关键发现

1. order_manager.py 的 place_order() 原本默认 order_type=”market“，违反阶段 2c 当前禁止市价单的要求。
2. 原逻辑在下单接口返回后立即将订单转为 LIVE，未先查订单详情确认状态。
3. OrderManager.__init__() 原本忽略 store.load() 返回的历史 halt 记录。
4. reconcile() 原本一开始就执行 self.halted = None，可能清除历史停机保护。
5. EXCHANGE_STATE_MAP 只包含 live、partially_filled、filled、canceled，未明确处理 mmp_canceled。
6. okx_demo.py 是只读客户端，没有下单或撤单方法，当前不应直接把它当作完整交易适配器。
7. okx_demo.py 的余额结果是原始数据字典，而 OrderManager 预期的余额结构是 {币种: 数值}；正式适配时必须设计并测试明确的规范化层。
8. 必须在最终网络发送边界检查 x-simulated-trading: 1，缺失或值错误就拒绝发送。
9. FakeExchange 的重复 clOrdId 拒绝行为是模拟假设，不代表已验证 OKX 的真实行为。
10. 需要区分可恢复的对账问题与必须人工解除的安全停机，不能让成功运行一次 reconcile() 就自动清除所有历史 halt。

三、GPT 最近对 order_manager.py 做过的修改

用户已按 GPT 指示修改文件，但没有提供修改后文件，也没有运行结果。请先读取用户当前工作区文件，不要假设内容与 GPT 方案完全一致。

修改意图包括：

1. 将 place_order() 默认参数从 order_type=”market“ 改成 order_type=None，未显式指定订单类型时抛出 ValueError。
2. 下单响应成功后先保持 SENT，再通过 get_order(clOrdId) 查询；查询异常或没有订单时转为 UNKNOWN 并停机。
3. 从 store.load() 恢复历史 halt。
4. 删除 reconcile() 开始时自动清除 self.halted 的行为，存在历史 halt 时返回失败。

这些修改仍有待审查的风险

* 第 2 项是否正确保留 SENT、合法转为 UNKNOWN，以及查询返回 state=live 时是否只在确认后转为 LIVE，必须结合当前代码验证。
* store.load() 返回的 halts 结构应以实际 order_store.py 为准。
* 当前方案尚未定义安全且持久化的人工解除停机流程。
* 只把默认类型改为 None，并没有彻底禁止显式 order_type=”market“。
* 旧测试可能因为不再隐式使用市价单而失败，需要更新测试调用，不能通过恢复不安全默认值来消除失败。
* 上述修改未执行回归测试，不得宣称已通过。

四、阶段 2c 硬性要求

安全约束

1. 只用 OKX 模拟盘；默认基址 https://openapi.okx.com，允许配置，但必须使用经过验证的 OKX 域名。
2. 每个请求必须带 x-simulated-trading: 1。缺失或错误时，必须在网络发送前 fail-closed。
3. 只用新建的模拟盘 API Key，具备必要的 Trade 权限。凭证沿用 2b 的安全存储规则，不进 Git。
4. 日志、测试输出和证据文件必须脱敏 Key、签名、Passphrase、UID。
5. 不碰杠杆、合约、市价单、WebSocket 或策略接入。市价单需要用户另行确认。
6. 默认 dry-run；只有显式 —confirm-demo 才允许发送真实模拟盘订单。
7. 请求串行，每秒不超过 2 次；对 50011 使用退避重试，不并发。
8. 只有在用户确认 D15 后才能进行真实模拟盘下单。未确认时，只允许代码开发与全假网络测试。

订单判定规则

* 外层 code=0 且订单响应 data[].sCode=0：只记录为 SENT，必须查询订单详情。
* 只有详情明确返回 state=live 才转为 LIVE。
* 任何非零 sCode 都按拒单处理，原样保留 sCode 和 sMsg，不得自行推断具体含义。
* 超时或响应不确定：进入 UNKNOWN，按 clOrdId 查询，绝不直接重发。
* 只识别 live、partially_filled、filled、canceled、mmp_canceled 五个状态；其他状态报警并停止交易。
* 成交量使用 accFillSz；取消状态也要核对累计成交量。
* clOrdId 由程序生成，唯一、仅字母数字、最多 32 位。
* 每次运行都从 instruments 读取 minSz、lotSz、tickSz，按交易规则处理数量与价格，不得硬编码为永久规则。

五、实测计划与当前状态

以下状态是交接时的已知状态，不代表 GPT 已执行完这些步骤。

步骤	状态	说明
1. 查询 instruments	已有历史 PASS，需重新运行确认	历史字段：minSz=0.00002、lotSz=0.00000001、tickSz=0.1
2. 查询模拟盘余额	已有历史 PASS，需重新运行确认	历史 USDT 可用余额为 4410.353540433386
3. 低于最新价约 2% 的限价买单	未执行	需要用户确认 D15，且代码审查与假网络测试先通过
4. 查询订单详情	未执行	目标确认 state=live、accFillSz=0
5. 撤单并再次查询	未执行	目标确认 state=canceled、accFillSz=0
6a. 数量低于 minSz	未执行	记录实际 sCode、sMsg
6b. 价格不是 tickSz 整数倍	未执行	记录实际 sCode、sMsg
6c. live 订单重复 clOrdId	未执行	记录实际响应；不要预设交易所一定如何处理
7. 超时恢复	假网络测试待复核	用假网络模拟请求超时，再按 clOrdId 查询恢复

不要把表格中的“未执行”写成“未通过”；也不要把历史只读成功写成真实下单成功。

六、优先工作顺序

1. 读取当前 order_manager.py、order_store.py 与测试文件，确认 GPT 修改的实际状态。
2. 修复订单状态确认、历史 halt 持久化、人工解除流程和市价单禁用。
3. 检查 OKX 响应解析：外层 code 与内层 sCode/sMsg 必须分开处理。
4. 设计交易所原始订单响应到内部规范订单结构的映射，包括 accFillSz 和 mmp_canceled。
5. 在最终发送网络请求的边界强制校验模拟盘 Header。
6. 为所有关键规则添加全假网络测试，并保留反向测试：
    * 把 sCode=0 错误当作已挂单；
    * 忽略 accFillSz；
    * 缺失模拟盘 Header 仍发送；
    * 超时直接当作失败；
    * clOrdId 不唯一。
7. 运行相关测试，记录实际通过数和失败详情。
8. 只有用户确认 D15、所有安全测试通过后，才开始真实模拟盘最小实测。
9. 将脱敏原始 JSON 摘要记录进 EXPERIMENTS.md，决策依据记录进 DECISIONS.md。
10. 最后给出 Git commit 一行摘要与简短详细说明。

七、交付要求

请不要只给分析或建议。用户希望拿到可执行的完整文件与明确操作步骤。

每次修改请提供：

* 完整可替换文件，而不是零散片段；
* 文件用途与修改原因；
* Pyto 中具体替换和运行步骤；
* 实际测试结果，不猜测；
* 每一步的 PASS / FAIL / NOT RUN；
* 失败时保留真实错误信息；
* Git commit 摘要与详细说明。

重要： 不要要求用户重复发送本报告中已完整提供过的源文件，优先使用当前项目工作区与已有上下文。若缺少修改后的文件状态，应明确指出并只请求最少必要信息。

八、GPT 当前交接结论

阶段 2c 的只读准备已有历史成功记录，但订单管理器刚经过未验证的局部修改。此时应先修复并验证代码，不能发送订单。

GPT 没有执行真实模拟盘下单、撤单或三项反向下单测试；没有生成实际运行的 EXPERIMENTS.md 证据，也没有完成最终提交。上述事项应全部视为待执行。