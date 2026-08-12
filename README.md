# 渊行 · 全品种期货四框架

单品种档案对已建库的 8 个有色/新能源品种增加极简基本面卡：只展示一句主要矛盾、3 个关键指标与完整研究框架跳转；不做基本面总评分。

原创深海远征视觉的期货技术决策网站，覆盖知几「观」API 支持的 77 个品种。

- Ari 动量环境
- 缠论程序化结构
- MACD 多级别共振
- 江恩八分位与 1×1 速度线

行情密钥只通过本地 config.local.json 或 GitHub Secret
ZHIJI_GUAN_KEY 注入，不进入静态文件。上游分钟线缺失或误返日线时，
网站会显示“降级观察”，不会伪装成完整多周期信号。

本地构建：

    python scripts/build_all.py --daily-proxy --workers 6
    python scripts/verify_site.py
