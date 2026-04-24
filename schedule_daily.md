# 每日定时跑批说明

config.ini 中已设置 `start_date = today`、`end_date = today`，每次运行时会**自动使用当天日期**作为时间筛选，无需改配置。

## Windows 定时任务

1. 打开 **任务计划程序**（taskschd.msc）
2. 创建基本任务 → 名称如「招投标采集每日跑批」→ 下一步
3. 触发器：**每天**，设置运行时间（如 早上 6:00）→ 下一步
4. 操作：**启动程序**
   - 程序：`python`
   - 参数：`run_pipeline.py`
   - 起始于：项目根目录（如 `E:\caiwu\caizhaowang`）
5. 完成。

或直接运行 `run_daily.bat` 测试；再用任务计划程序把该批处理设为每天执行。

## Linux / macOS（cron）

```bash
# 每天 6:00 执行（需改成你的项目路径）
0 6 * * * cd /path/to/caizhaowang && python run_pipeline.py
```

编辑 crontab：`crontab -e`，添加上面一行。
