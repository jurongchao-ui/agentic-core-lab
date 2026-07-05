"""embedding — 长期记忆的本地向量检索边界。

这个模块先提供标准库版 HashingMemoryEmbeddingIndex:
  - 不依赖外部 embedding 模型。
  - 用 token hashing 构造稀疏向量。
  - 用 cosine similarity 给 query 和 memory 排序。

它的价值不在于“语义能力很强”,而在于把 MemoryStore 和具体向量后端解耦。
生产里可以把这个类替换成 OpenAI embeddings + pgvector / Milvus / Redis Vector。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from agentic_core.runtime.schemas import MemoryRecord


class MemoryEmbeddingIndex(Protocol):
    """长期记忆向量索引协议。"""

    def search(
        self,
        query: str,
        memories: list[MemoryRecord],
        limit: int | None = None,
        min_score: float = 0.0,
    ) -> list[MemoryRecord]:
        ...


@dataclass(frozen=True)
class MemorySearchMatch:
    """单条记忆的相似度结果。"""

    memory: MemoryRecord
    score: float


class HashingMemoryEmbeddingIndex:
    """标准库版 hashing embedding index。

    这是一个 deterministic learning backend。它不会调用 LLM,所以适合测试、回归和离线学习。
    """

    def __init__(self, dimensions: int = 4096) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def search(
        self,
        query: str,
        memories: list[MemoryRecord],
        limit: int | None = None,
        min_score: float = 0.0,
    ) -> list[MemoryRecord]:
        return [match.memory for match in self.search_with_scores(query, memories, limit, min_score)]

    def search_with_scores(
        self,
        query: str,
        memories: list[MemoryRecord],
        limit: int | None = None,
        min_score: float = 0.0,
    ) -> list[MemorySearchMatch]:
        query_vector = self.embed(query)
        matches: list[MemorySearchMatch] = []
        for memory in memories:
            score = _cosine_similarity(query_vector, self.embed(_memory_embedding_text(memory)))
            if score >= min_score:
                matches.append(MemorySearchMatch(memory=memory, score=score))
        matches.sort(
            key=lambda match: (
                match.score,
                match.memory.importance,
                match.memory.access_count,
                match.memory.updated_at or match.memory.created_at,
            ),
            reverse=True,
        )
        return matches[:limit] if limit is not None else matches

    def embed(self, text: str) -> dict[int, float]:
        vector: dict[int, float] = {}
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] = vector.get(index, 0.0) + 1.0
        return vector


def _memory_embedding_text(memory: MemoryRecord) -> str:
    return f"{memory.memory_type} {memory.text} {memory.reason}"


def _tokens(text: str) -> list[str]:
    normalized = text.lower()
    tokens: list[str] = []
    for match in re.finditer(r"[a-z0-9_.+-]+|[\u4e00-\u9fff]+", normalized):
        value = match.group(0)
        if _is_cjk(value):
            tokens.extend(_cjk_ngrams(value))
        else:
            tokens.append(value)
    return tokens


def _is_cjk(value: str) -> bool:
    return all("\u4e00" <= char <= "\u9fff" for char in value)


def _cjk_ngrams(value: str) -> list[str]:
    if len(value) <= 1:
        return [value]
    grams: list[str] = list(value)
    grams.extend(value[index : index + 2] for index in range(0, len(value) - 1))
    if len(value) >= 3:
        grams.extend(value[index : index + 3] for index in range(0, len(value) - 2))
    if len(value) <= 8:
        grams.append(value)
    return grams


def _cosine_similarity(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(index, 0.0) for index, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
