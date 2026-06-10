Codex 优化审计报告
以下是逐项审查结果。
￼
✅ 改动 1：轮询间隔 2s → 10s
评价：正确。 后端扫描间隔 180s，前端 2s 重绘毫无意义，10s 是合理的折中。
￼
✅ 改动 2：后端 data_version + 前端签名去重
评价：思路正确，但实现有 bug。
Copy code to clipboard
// App.jsx 第 55-82 行
const fetchData = useCallback(async ({ force = false, signal } = {}) => {
    if (dataRequestRef.current && !force) return;   // ← BUG: 新请求直接吞掉
    // ...
    const requestId = Symbol('data-request');
    dataRequestRef.current = requestId;
    // ...
    if (dataRequestRef.current !== requestId) return; // ← 这个检查永远为 true
问题：
• 第一个 if 分支：当一个请求正在 await 时，第二个请求调用 fetchData 会 直接 return，根本不发出请求。
• 第二个 if 分支：因为 dataRequestRef.current 在 await 之后才被设为 null，所以它永远等于 requestId，这个检查形同虚设。
• Symbol 每次都不同，=== 永远成立，finally 块里也永远能清空。
修复建议：
Copy code to clipboard
const abortControllerRef = useRef(null);

const fetchData = useCallback(async ({ force = false } = {}) => {
    // 取消上一个请求
    abortControllerRef.current?.abort();
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
        const response = await fetch('/api/data', { 
            cache: 'no-store', 
            signal: controller.signal 
        });
        // ...
        setData((prevData) => {
            if (!force && getDashboardSignature(prevData) === getDashboardSignature(nextData)) {
                return prevData;
            }
            return nextData;
        });
    } catch (err) {
        if (err.name === 'AbortError') return;
        setError('...');
    }
}, []); // 空依赖，因为用 ref 管理取消
￼
✅ 改动 3：useMemo 包裹过滤/排序/可见行
评价：方向正确，但最大计算热点未处理。
已正确 memo 的：allPossibleExchanges、enabledExchanges、enabledExchangeSet、oppsBySymbol、sortedSymbols、visibleSymbols。
核心性能瓶颈在 symbolsWithDelta（第 180-222 行）——这里每次计算时对 symbolOpps 做 reduce，其中对每个 opportunity 调用 total_apr 或复算 APR 公式。
Copy code to clipboard
// 第 188-192 行：每次 reduce 都重新算
symbolOpps.reduce((prev, curr) => {
    const aprP = prev.total_apr || ((prev.details.funding_diff_scaled_bps * ...));
    const aprC = curr.total_apr || ((curr.details.funding_diff_scaled_bps * ...));
    return aprP > aprC ? prev : curr;
})
100 个 symbol × 平均 5 个 opportunity × 每次计算两个 APR 表达式。这段代码在 useMemo 里虽然只跑一次，但公式本身写得很冗长。真正的卡顿元凶是第 321-422 行的表格 render ——100 行全量渲染，而 calcDailyYieldPercent 在 JSX 里被调用了 3 次/行。
￼
✅ 改动 4：移除 Google Fonts @import
评价：正确。 国内网络访问 Google Fonts 极慢或不通，阻塞首屏渲染。改用系统字体栈正确。
￼
✅ 改动 5：PositionsModal hooks warning 修复
评价：部分正确，lint 警告消除了，但有新隐患。
handleDelete 使用了 username 和 password，但没有在依赖数组里声明（第 130 行）。虽然闭包里 authRef 间接解决了值的问题，但这是 React 规则错误的漏网之鱼。
￼
🚨 剩余性能问题（未修复）
优先级
问题
位置
建议
P0
表格 100 行全量渲染，无虚拟化
App.jsx 321-422
用 react-window 或 react-virtualized 只渲染可见行（当前 viewport 通常 ≤ 20 行）
P0
calcDailyYieldPercent 在 JSX 里重复调用 3 次/行
App.jsx 292, 302, 388
将结果存入 visibleSymbols 的 memo 数据里
P1
symbolsWithDelta里的 APR reduce重复计算
App.jsx 188-192
提前用 total_apr（后端已算好），跳过公式重算
P1
fetchData 请求去重 bug
App.jsx 55
按上面建议修复
P2
arb-container 用 IIFE inline 渲染
App.jsx 367
提取成 React.memo 子组件 <ArbitrageCell />
P2
PositionsModal 里的 fmtUsd 等工具函数每次 render 重建
PositionsModal.jsx 154-173
提到组件外部作为纯函数
P2
CSS 重复定义 .arb-breakdown（两处）
index.css 755, 969
合并
￼
总结
这次优化解决了正确方向的几个问题（轮询间隔、字体加载、基础 memo），但真正的卡顿根源（100 行表格全量渲染 + JSX 内重复计算）没有触及。 如果修复后仍然卡顿，表格虚拟化是第一优先项——它能把 DOM 节点从 ~2000 个降到 ~200 个，效果会非常显著。