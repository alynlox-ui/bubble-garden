# 🫧 泡泡花园 · Bubble Garden

H5 泡泡射击休闲游戏（单文件，双击即玩）。

## 特色
- **5 大章节 × 50 关**：春之芽 → 夏之花 → 秋之实 → 冬之霜 → 星之冠
- 泡泡数加速曲线 **6 → 75**（前期温和、后期陡增）
- 特殊泡泡：彩虹 / 炸弹 / 闪电 / 冰块 / 石头 + 连锁引擎
- 上瘾系统：连击倍率、连胜加成、最佳纪录、章节通关奖励、总星数收集
- 创意工坊：自制关卡编辑器 + 保存/试玩
- 每关数学可通关（限步 ≥ 理论最少发数 +4），死色过滤 + 多数色保底

## 运行
直接用浏览器打开 `index.html`，或部署任意静态托管。

## 部署（Render）
仓库自带 `render.yaml` Blueprint（static runtime）：
`buildCommand: python3 prepare_web_dist.py` → `staticPublishPath: ./web-dist`

## 验证
`python verify_demo.py`：Headless Edge + CDP，39 项逻辑自检。
