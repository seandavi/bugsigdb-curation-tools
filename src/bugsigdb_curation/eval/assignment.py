"""Pure-Python optimal bipartite assignment (Hungarian / Kuhn-Munkres algorithm).

Used by :mod:`bugsigdb_curation.eval.score` to align predicted experiments to
gold experiments, and predicted signatures to gold signatures, by a
field-overlap / taxa-overlap score. No numpy/scipy: experiment counts in this
corpus run 1-200 and signature counts per experiment are almost always <= a
handful, so a plain O(n^2 * m) pure-Python implementation is fast enough and
keeps this package dependency-light (per the eval-harness build brief).
"""

from __future__ import annotations

_INF = float("inf")


def _solve_min_cost(cost: list[list[float]]) -> list[int]:
    """Rectangular Hungarian algorithm (``n_rows <= n_cols``), minimizing total cost.

    Returns a list ``p`` of length ``n_rows``: ``p[i]`` is the assigned column
    index for row ``i``. Every row is assigned a column (since ``n_rows <=
    n_cols`` a feasible perfect matching on the row side always exists).

    This is the standard O(n^2 * m) shortest-augmenting-path-with-potentials
    formulation of the Hungarian algorithm (the well-known 1-indexed
    competitive-programming layout; see e.g. e-maxx.ru / Wikipedia's
    "Hungarian algorithm" O(n^3) variant, generalized here to rectangular
    ``n <= m`` matrices rather than requiring a square one).
    """
    n = len(cost)
    m = len(cost[0]) if n else 0
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j] = row currently assigned to column j (1-indexed); 0 = free
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [_INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = _INF
            j1 = -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        # Augment along the alternating path found.
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    result = [0] * (n + 1)
    for j in range(1, m + 1):
        if p[j] != 0:
            result[p[j]] = j
    return [result[i] - 1 for i in range(1, n + 1)]


def assign_max_weight(scores: list[list[float]]) -> list[int | None]:
    """Optimal bipartite matching maximizing total score.

    ``scores[i][j]`` is the match quality between row ``i`` and column ``j``
    (higher is better, e.g. a field-overlap score in ``[0, 1]``). Returns a
    list of length ``len(scores)``: ``result[i]`` is the matched column index
    for row ``i``, or ``None`` if row ``i`` is left unmatched (only possible
    when there are more rows than columns -- some rows necessarily go
    unmatched since there aren't enough columns to pair with).

    Every column is used at most once. This is a *complete* assignment on
    whichever side is smaller; callers that need a "no good match" concept
    (e.g. experiment alignment, where a predicted/gold pair with near-zero
    overlap shouldn't count as "matched") should apply their own minimum-score
    threshold to the returned pairs -- this function only guarantees
    optimality of the *sum* of matched scores, not that every match is a good
    one.
    """
    n_rows = len(scores)
    n_cols = len(scores[0]) if n_rows else 0
    if n_rows == 0 or n_cols == 0:
        return [None] * n_rows

    if n_rows <= n_cols:
        cost = [[-s for s in row] for row in scores]
        assignment = _solve_min_cost(cost)
        return [j if j >= 0 else None for j in assignment]

    # More rows than columns: solve the transposed (cols <= rows) problem
    # instead, then invert to a per-row assignment. Rows beyond the column
    # count are necessarily left unmatched -- there just aren't enough columns.
    cost_t = [[-scores[i][j] for i in range(n_rows)] for j in range(n_cols)]
    col_assignment = _solve_min_cost(cost_t)  # length n_cols, values = row idx
    row_to_col: list[int | None] = [None] * n_rows
    for col_idx, row_idx in enumerate(col_assignment):
        if row_idx >= 0:
            row_to_col[row_idx] = col_idx
    return row_to_col
