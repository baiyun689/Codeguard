"""兼容部分 OpenAI 端点把数组参数序列化为 JSON 字符串。"""

from __future__ import annotations

import json

from codeguard_agent.pipeline.council.concern import _ParsedClaimBatch
from codeguard_agent.pipeline.evidence.verifier import _RelationBatch


def test_claim_and_relation_batches_accept_stringified_arrays():
    claims = _ParsedClaimBatch.model_validate(
        {
            "claims": json.dumps(
                [{"candidate_id": "candidate-1", "root_cause": "错误机制"}],
                ensure_ascii=False,
            )
        }
    )
    relations = _RelationBatch.model_validate(
        {
            "findings": json.dumps(
                [
                    {
                        "fact_id": "fact-1",
                        "relation": "supports",
                        "strength": "direct",
                        "observation": "代码直接调用危险 API",
                    }
                ],
                ensure_ascii=False,
            )
        }
    )

    assert claims.claims[0].candidate_id == "candidate-1"
    assert relations.findings[0]["fact_id"] == "fact-1"
