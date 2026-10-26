"""QuantDesk 表现层（Streamlit）。

分层与约定（ARCHITECTURE §2.1/§2.2、决策 D-9）：

- 入口：项目根目录的 ``app.py``（导航 + 页面动态发现 + 全局外壳）。
- 页面：``ui/pages/*.py``，每个文件即一个页面，必须暴露 ``PAGE_TITLE`` 与
  ``render()``；可选 ``PAGE_ICON``、``PAGE_ORDER``。新增页面只加文件，不改 app.py。
- 共享组件：``ui/pages/_common.py``（CF-12：二次确认、中文错误卡片、金额/涨跌格式、
  页面标题栏、数据未就绪占位）。

约束：本包只做「展示 + 交互」，不查库、不计算指标、不直接调用 Broker；
所有业务数据都经由服务层（PortfolioService / OrderService / BacktestEngine /
SchedulerService / BarCache 等）获取，具体接口以 SPEC §6 接口契约为准。
"""

__all__ = ["pages"]
