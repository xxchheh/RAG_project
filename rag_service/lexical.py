"""Pinned open-source Chinese segmentation + BM25, isolated from rule execution."""
from functools import lru_cache


STOP = set("的 了 和 与 及 是 在 如何 什么 哪些 为什么 怎么 怎样 有 哪个 一个 吗 呢 请 帮 我 进行 相关 有什么 是否".split())

def tokens(text):
    import jieba
    # Only unambiguous equipment spelling is normalized; never infer a fault.
    text = text.replace('电动机', '电机')
    return [word.lower() for word in jieba.lcut(text) if word.strip() and word not in STOP and any(c.isalnum() for c in word)]


@lru_cache(maxsize=8)
def _index(corpus):
    from rank_bm25 import BM25Plus
    return BM25Plus([list(row) or ['__empty__'] for row in corpus])


def scores(query, rows):
    if not rows: return []
    return _index(tuple(tuple(row) for row in rows)).get_scores(tokens(query)).tolist()
