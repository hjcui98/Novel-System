"""Per-channel query compilation for Stage1MemoryNeed."""

from __future__ import annotations

from novel_agent.domain.memory import RetrievalChannel, Stage1MemoryNeed
from novel_agent.domain.planning_memory import RetrievalQueryBundle


class NeedQueryCompiler:
    """Compile one Need into channel-specific queries (Phase 2).

    The compiled bundle separates the semantic question from lexical
    reformulations and exact structured filters, so BM25, dense, R1, and graph
    channels no longer share one ``query_text``.  The executed channel set is
    the intersection of ``ROUTES[query_intent]`` with the queries this bundle
    provides.
    """

    version = "need_query_compiler.v2"

    def compile(self, need: Stage1MemoryNeed) -> RetrievalQueryBundle:
        semantic_query = need.semantic_question or need.query_text
        lexical_queries = tuple(
            dict.fromkeys(query for query in (need.query_text, *need.query_hints) if query.strip())
        ) or (need.query_text,)
        excluded_information_labels = () if need.retrieval_may_return_plan else ("plan",)
        # Graph predicates are compiled from the Need's public predicates
        # (registry names, e.g. ``mentor_of``) instead of a hard-coded empty
        # set; unresolved lexical anchors keep lexical/dense eligible while
        # exact/graph fail closed on the missing seed (eligible_channels).
        graph_relations = tuple(dict.fromkeys(need.predicates))
        return RetrievalQueryBundle(
            semantic_query=semantic_query,
            lexical_queries=lexical_queries,
            exact_entity_ids=need.entity_ids,
            exact_predicates=need.predicates,
            graph_seeds=need.entity_ids,
            graph_relations=graph_relations,
            time_scope=need.time_scope,
            excluded_information_labels=excluded_information_labels,
        )

    @staticmethod
    def eligible_channels(
        need: Stage1MemoryNeed,
        bundle: RetrievalQueryBundle,
        channels: tuple[RetrievalChannel, ...],
    ) -> tuple[tuple[RetrievalChannel, ...], dict[RetrievalChannel, str]]:
        """Return channels with an executable compiled query and typed exclusions."""

        eligible: list[RetrievalChannel] = []
        unavailable: dict[RetrievalChannel, str] = {}
        for channel in channels:
            reason: str | None = None
            if channel in {RetrievalChannel.ANCHOR_BM25, RetrievalChannel.GROUNDED_BM25}:
                if not any(query.strip() for query in bundle.lexical_queries):
                    reason = "missing_lexical_query"
            elif channel in {RetrievalChannel.ANCHOR_DENSE, RetrievalChannel.GROUNDED_DENSE}:
                if not bundle.semantic_query.strip():
                    reason = "missing_semantic_query"
            elif channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
                if not (bundle.exact_entity_ids or bundle.exact_predicates):
                    reason = "missing_exact_entity_or_predicate"
            elif channel is RetrievalChannel.TYPED_GRAPH:
                if not bundle.graph_seeds:
                    reason = "missing_graph_seed"
            elif channel is RetrievalChannel.HIERARCHY and not need.hierarchy_parent_unit_ids:
                reason = "missing_hierarchy_basis"
            if reason is None:
                eligible.append(channel)
            else:
                unavailable[channel] = reason
        return tuple(eligible), unavailable


def compile_need_query(need: Stage1MemoryNeed) -> RetrievalQueryBundle:
    """Module-level convenience for corridor backends."""
    return NeedQueryCompiler().compile(need)


__all__ = ["NeedQueryCompiler", "compile_need_query"]
