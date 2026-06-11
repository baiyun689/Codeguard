"""包入口:支持 `python -m codeguard_agent` 直接运行。"""

from codeguard_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
