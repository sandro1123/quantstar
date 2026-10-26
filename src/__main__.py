"""QuantStar 入口 — 委托给 Click CLI"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 尽早加载 .env —— 保证所有 `python -m src ...` 入口(含全部 cron 管道)在导入
# 任何业务模块前就拿到密钥 (FUYAO_API_KEY 等)。cron 不继承 shell 环境; 若只依赖
# bootstrap_app() 内部才 load_dotenv, 任何在 bootstrap 之前构造的客户端都会读到
# 空 key (2026-08-17 fuyao_financial "FUYAO_API_KEY 未设置" 的结构性根因)。
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from cli.main import cli

if __name__ == "__main__":
    cli()
