"""候选代码片段定位的稳定包级接口。"""

from codeguard_agent.pipeline.location.locator import (
    LocationBatch,
    LocationRecord,
    locate_issues,
)

__all__ = ["LocationBatch", "LocationRecord", "locate_issues"]
