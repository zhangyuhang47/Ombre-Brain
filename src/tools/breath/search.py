"""
========================================
tools/breath/search.py — 有 query 的检索模式
========================================

走 breath(query=...) 时进入这里。一次向量查询与 bucket_manager 的
关键词/BM25 检索融合，逐条 dehydrate 后塞 token 预算。

关键行为：
- domain/valence/arousal 作为过滤参数传给 bucket_mgr.search
- embedding 未配置/未启用/调用失败时明确提示并继续关键词/BM25 检索
- 向量通道阈值 sim>=0.65；domain/tags/type 过滤与关键词通道完全一致
- 脱水 API 失败时返回最多 300 字原文片段，不让展示层故障吞掉命中
- 命中后调 touch()，记忆重构会把展示层 valence 按当前情绪做 ±0.1 微调
- 检索结果 < 3 时 40% 概率从低权重旧桶里随机漂出 1-3 条「忽然想起来」
- 命中 0 条时回 webhook 报空，并给出可操作的引导文案

不做什么（边界）：
- 不返回 feel/plan/letter（专用通道有自己的入口）
- pinned/protected/permanent 仍可被检索（也是记忆，只是同时在浮现模式置顶）
- dont_surface=True 在检索中保留——主动遗忘只限制无参浮现

对外暴露：surface_search(query, max_results, max_tokens, domain, valence,
                          arousal, tag_filter) → str
========================================
"""

import asyncio
import random

from ombrebrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from utils import strip_wikilinks, count_tokens_approx

_SURFACE_POLICY = SurfacePolicyVM.default()

# Bound concurrent summaries so a cold cache does not burst the provider.
_DEHY_CONCURRENCY = 5
_VECTOR_QUERY_TOPK = 50

_SEMANTIC_DISABLED_NOTE = "[检索降级：语义索引暂不可用，本次仅使用关键词/BM25。]"
_SUMMARY_FALLBACK_NOTE = "[展示降级：摘要服务暂不可用，以下为原文片段。]"


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface_search(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="search").allowed


def _raw_summary_fallback(content: str) -> str:
    return strip_wikilinks(content)[:300].strip() or "（空记忆）"


async def _semantic_scores(query: str, top_k: int) -> tuple[dict[str, float], str]:
    """Run the vector query once and return scores plus an optional notice."""
    engine = rt.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        rt.logger.warning("breath semantic search unavailable; using keyword/BM25 only")
        return {}, _SEMANTIC_DISABLED_NOTE

    try:
        strict_search = getattr(engine, "search_similar_strict", None)
        if callable(strict_search):
            pairs = await strict_search(query, top_k=top_k)
        else:
            pairs = await engine.search_similar(query, top_k=top_k)
        return {bucket_id: float(score) for bucket_id, score in pairs}, ""
    except Exception as exc:
        rt.logger.warning(
            f"breath semantic search failed; using keyword/BM25 only: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, _SEMANTIC_DISABLED_NOTE


async def surface_search(
    query: str,
    max_results: int,
    max_tokens: int,
    domain: str,
    valence: float,
    arousal: float,
    tag_filter: list,
) -> str:
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    vector_scores, semantic_notice = await _semantic_scores(
        query, top_k=max(max_results, _VECTOR_QUERY_TOPK)
    )

    try:
        matches = await rt.bucket_mgr.search(
            query,
            limit=max(max_results, 20),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            vector_scores=vector_scores,
        )
    except Exception as e:
        rt.logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    matches = [
        b for b in matches
        if _can_surface_search(b)
        and b["metadata"].get("type") not in ("feel", "plan", "letter")
    ]
    matches = [b for b in matches if _bucket_has_tags(b["metadata"], tag_filter)]
    matches = matches[:max_results]

    # 性能 P3：候选桶并发脱水（有界信号量），再按原顺序套 token 预算。
    # 冷缓存时 N 次 LLM 往返从串行变并发；matches 已被上游截到 ~20，量可控。
    _sem = asyncio.Semaphore(_DEHY_CONCURRENCY)

    async def _dehydrate_one(bucket):
        """返回 (is_core, is_vector, bucket_id, summary, used_raw_fallback)。"""
        meta_b = bucket["metadata"]
        is_core = meta_b.get("pinned") or meta_b.get("protected") or meta_b.get("type") == "permanent"
        clean_meta = {k: v for k, v in meta_b.items() if k != "tags"}
        # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
        if q_valence is not None and "valence" in clean_meta:
            original_v = float(clean_meta.get("valence") or 0.5)
            shift = (q_valence - 0.5) * 0.2
            clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
        async with _sem:
            try:
                summary = await rt.dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            except Exception as dehy_err:
                rt.logger.warning(
                    f"search result dehydrate failed, using raw fallback: "
                    f"{type(dehy_err).__name__}: {dehy_err}"
                )
                summary = _raw_summary_fallback(bucket["content"])
                used_raw_fallback = True
            else:
                used_raw_fallback = False
        if not str(summary or "").strip():
            summary = _raw_summary_fallback(bucket["content"])
            used_raw_fallback = True
        return (
            is_core,
            bool(bucket.get("vector_match")),
            bucket["id"],
            summary,
            used_raw_fallback,
        )

    dehydrated = await asyncio.gather(*[_dehydrate_one(b) for b in matches])

    results = []
    token_used = 0
    used_raw_fallback = False
    touched_ids: list = []   # 性能 P2：浮现后统一在后台 touch，不在响应路径逐条 await
    for item in dehydrated:
        if item is None:
            continue
        if token_used >= max_tokens:
            break
        is_core, is_vector, bucket_id, summary, item_used_raw = item
        used_raw_fallback = used_raw_fallback or item_used_raw
        summary_tokens = count_tokens_approx(summary)
        if token_used + summary_tokens > max_tokens:
            break
        touched_ids.append(bucket_id)
        if is_core:
            summary = f"📌 [核心准则] [bucket_id:{bucket_id}] {summary}"
        elif is_vector:
            summary = f"[语义关联] [bucket_id:{bucket_id}] {summary}"
        else:
            summary = f"[bucket_id:{bucket_id}] {summary}"
        results.append(summary)
        token_used += summary_tokens

    # 性能 P2：把 touch 移出响应路径 —— 浮现完的桶在后台一次性更新激活，
    # ripple=False 跳过读全库的时间涟漪。响应不再等这些写盘/涟漪。
    if touched_ids:
        asyncio.create_task(rt.bucket_mgr.touch_many(touched_ids, ripple=False))

    # --- 检索结果 < 3 时 40% 概率随机浮现 ---
    if len(matches) < min(3, max_results) and random.random() < 0.4:
        try:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and rt.decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                remaining_slots = max(0, max_results - len(matches))
                drifted = random.sample(
                    low_weight,
                    min(random.randint(1, 3), len(low_weight), remaining_slots),
                )
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await rt.dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            rt.logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        if rt.fire_webhook:
            await rt.fire_webhook("breath", {"mode": "empty", "matches": 0})
        empty_text = (
            f"没有匹配到「{query}」相关的记忆。\n"
            "可以换个关键词试试，或不带 query 看当下权重池；feel 用 breath(domain=\"feel\")，信件用 letter_read。"
        )
        return f"{semantic_notice}\n{empty_text}" if semantic_notice else empty_text

    final_text = "\n---\n".join(results)
    notices = []
    if semantic_notice:
        notices.append(semantic_notice)
    if used_raw_fallback:
        notices.append(_SUMMARY_FALLBACK_NOTE)
    if notices:
        final_text = "\n".join(notices + [final_text])
    if rt.fire_webhook:
        await rt.fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text
