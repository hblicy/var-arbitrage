这个帮我优化一下看，
D:\code-web3\web3-bot\DEX\var-arbitrage_v1.6
页面会卡顿一下

优化好了，主要处理了页面“隔一会儿顿一下”的几个热点：

- [App.jsx](D:/code-web3/web3-bot/DEX/var-arbitrage_v1.6/dashboard/src/App.jsx:55)：轮询从每 2 秒重复刷新改成 10 秒，并且同版本数据不再 `setState`，避免整张 100 行表格反复重渲染。
- [api.py](D:/code-web3/web3-bot/DEX/var-arbitrage_v1.6/api.py:147)：后端 `/api/data` 增加 `data_version`，前端可以准确判断数据是否真的更新。
- [App.jsx](D:/code-web3/web3-bot/DEX/var-arbitrage_v1.6/dashboard/src/App.jsx:147)：交易所过滤、机会分组、排序和表格可见行都做了稳定 memo，减少计算抖动。
- [index.css](D:/code-web3/web3-bot/DEX/var-arbitrage_v1.6/dashboard/src/index.css:19)：移除了 Google Fonts `@import`，改用系统字体，避免国内网络下字体加载/切换导致首屏卡顿。
- [PositionsModal.jsx](D:/code-web3/web3-bot/DEX/var-arbitrage_v1.6/dashboard/src/PositionsModal.jsx:1)：修掉 Hooks 依赖 warning，并清理了一些乱码按钮/箭头文本。

验证已通过：

```text
npm run lint
npm run build
python -m py_compile api.py
```

这版应该能明显减少那种定时“顿一下”的感觉，尤其是后端实际 180 秒才扫描一次的情况下，之前前端 2 秒重绘一次确实有点太勤快了。