"""Watch-order organization for anime franchises.

Groups resolved anime entries by franchise (using AniList relations) and
assigns a sequential watch_order_index within each group.

Unknown stays unknown: if no AniList relations are available, watch_order_group
and watch_order_index are None.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

LOGGER = logging.getLogger("TelegramStremio.WatchOrder")

# Try to import AniList provider; fall back to None if Backend stack unavailable
try:
    from Backend.helper.metadata.providers import anilist as _anilist
    _ANILIST_AVAILABLE = True
except Exception:
    _anilist = None
    _ANILIST_AVAILABLE = False

# Relation types that define watch order direction
_PREQUEL_TYPES = {"PREQUEL"}
_SEQUEL_TYPES = {"SEQUEL"}


def _anime_key(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return a stable (id_type, id_value) key for an entry."""
    anilist_id = entry.get("anilist_id")
    mal_id = entry.get("mal_id")
    if anilist_id:
        return ("anilist", str(anilist_id))
    if mal_id:
        return ("mal", str(mal_id))
    return None


def _entry_title(entry: Dict[str, Any]) -> str:
    """Get a display title for an entry."""
    return entry.get("title") or entry.get("title_english") or "Unknown"


def _entry_year(entry: Dict[str, Any]) -> Optional[int]:
    """Get the year for an entry."""
    y = entry.get("year") or entry.get("season_year")
    if y is not None:
        try:
            return int(y)
        except (TypeError, ValueError):
            return None
    return None


def _entry_season(entry: Dict[str, Any]) -> Optional[int]:
    """Get the season number for an entry."""
    s = entry.get("season_number")
    if s is not None:
        try:
            return int(s)
        except (TypeError, ValueError):
            return None
    return None


class WatchOrderBuilder:
    """Builds watch-order groups from resolved anime entries and AniList relations."""

    def __init__(self, entries: List[Dict[str, Any]]):
        self.entries = entries
        # key -> entry index
        self._key_to_index: Dict[Tuple[str, str], int] = {}
        for i, entry in enumerate(entries):
            key = _anime_key(entry)
            if key:
                self._key_to_index[key] = i
        # key -> set of neighbor keys (franchise graph)
        self._graph: Dict[Tuple[str, str], Set[Tuple[str, str]]] = defaultdict(set)
        # ordering edges (directed): (before_key, after_key)
        self._order_edges: List[Tuple[Tuple[str, str], Tuple[str, str]]] = []

    async def fetch_relations(self) -> None:
        """Fetch AniList relations for all entries that have an anilist_id."""
        if not _ANILIST_AVAILABLE or not _anilist:
            LOGGER.debug("[WatchOrder] AniList provider unavailable; skipping relations fetch")
            return

        for i, entry in enumerate(self.entries):
            anilist_id = entry.get("anilist_id")
            if not anilist_id:
                continue
            try:
                relations = await _anilist.get_anilist_relations(int(anilist_id))
            except Exception as e:
                LOGGER.debug(f"[WatchOrder] Relations fetch failed for anilist_id {anilist_id}: {e}")
                relations = None
            if not relations:
                continue

            source_key = ("anilist", str(anilist_id))
            for rel in relations:
                rel_type = rel.get("relation_type")
                rel_id = rel.get("anilist_id")
                if not rel_type or not rel_id:
                    continue

                target_key = ("anilist", str(rel_id))

                # Add to franchise graph (undirected for component detection)
                self._graph[source_key].add(target_key)
                self._graph[target_key].add(source_key)

                # Add to ordering edges (directed)
                if rel_type in _PREQUEL_TYPES:
                    # source is prequel of target -> source comes before target
                    self._order_edges.append((source_key, target_key))
                elif rel_type in _SEQUEL_TYPES:
                    # source is sequel of target -> target comes before source
                    self._order_edges.append((target_key, source_key))

    def _connected_components(self) -> List[Set[Tuple[str, str]]]:
        """Find connected components in the franchise graph."""
        visited: Set[Tuple[str, str]] = set()
        components: List[Set[Tuple[str, str]]] = []

        for node in self._graph:
            if node in visited:
                continue
            component: Set[Tuple[str, str]] = set()
            queue = deque([node])
            visited.add(node)
            while queue:
                current = queue.popleft()
                component.add(current)
                for neighbor in self._graph.get(current, set()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            components.append(component)

        return components

    def _topological_sort(self, component: Set[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Topologically sort a component using prequel/sequel edges.

        Falls back to season_year and title for ties.
        """
        # Build adjacency within this component
        in_degree: Dict[Tuple[str, str], int] = {node: 0 for node in component}
        adj: Dict[Tuple[str, str], List[Tuple[str, str]]] = {node: [] for node in component}

        for before, after in self._order_edges:
            if before in component and after in component:
                adj[before].append(after)
                in_degree[after] += 1

        # Kahn's algorithm with season_year tie-breaking
        entry_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for i, entry in enumerate(self.entries):
            key = _anime_key(entry)
            if key:
                entry_by_key[key] = entry

        def sort_key(key: Tuple[str, str]) -> Tuple:
            entry = entry_by_key.get(key, {})
            return (_entry_year(entry) or 0, _entry_title(entry).lower(), str(key))

        ready = sorted(
            [node for node in component if in_degree[node] == 0],
            key=sort_key,
        )
        result: List[Tuple[str, str]] = []

        while ready:
            node = ready.pop(0)
            result.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    ready.append(neighbor)
                    ready.sort(key=sort_key)

        # If there's a cycle, append remaining nodes unsorted
        if len(result) < len(component):
            remaining = component - set(result)
            result.extend(sorted(remaining, key=sort_key))

        return result

    def build(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Assign watch_order_group and watch_order_index to each entry key.

        Returns a dict mapping entry key -> {"watch_order_group": str, "watch_order_index": int}.
        Entries not in any franchise group are not included.
        """
        components = self._connected_components()
        assignments: Dict[Tuple[str, str], Dict[str, Any]] = {}

        for group_idx, component in enumerate(components, start=1):
            component_entries = [k for k in component if k in self._key_to_index]
            if len(component_entries) < 2:
                continue

            sorted_keys = self._topological_sort(component)
            indexed_keys = [k for k in sorted_keys if k in self._key_to_index]

            group_id = f"franchise_{group_idx}"
            for order_idx, key in enumerate(indexed_keys, start=1):
                assignments[key] = {
                    "watch_order_group": group_id,
                    "watch_order_index": order_idx,
                }

        return assignments

    async def run(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Fetch relations and build watch-order assignments."""
        await self.fetch_relations()
        return self.build()


async def assign_watch_order(entries: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Convenience function: build and run watch-order assignment."""
    builder = WatchOrderBuilder(entries)
    return await builder.run()


def apply_watch_order(
    payload: Dict[str, Any],
    assignments: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply watch_order_group and watch_order_index to a payload.

    Purely additive: only sets fields if the entry's key is in assignments.
    Never overwrites existing values.
    """
    key = _anime_key(payload)
    if not key:
        return payload

    assignment = assignments.get(key)
    if not assignment:
        return payload

    if payload.get("watch_order_group") is None:
        payload["watch_order_group"] = assignment.get("watch_order_group")
    if payload.get("watch_order_index") is None:
        payload["watch_order_index"] = assignment.get("watch_order_index")

    return payload
