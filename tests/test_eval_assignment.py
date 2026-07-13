"""Unit tests for bugsigdb_curation.eval.assignment -- the pure-Python Hungarian
(Kuhn-Munkres) optimal bipartite assignment."""

from __future__ import annotations

from bugsigdb_curation.eval.assignment import _solve_min_cost, assign_max_weight


def _total_score(scores: list[list[float]], assignment: list[int | None]) -> float:
    return sum(scores[i][j] for i, j in enumerate(assignment) if j is not None)


def test_assign_max_weight_finds_optimum_where_greedy_would_fail():
    # The classic greedy trap: picking each row's own best column independently
    # (row 0 -> col 0 = 10, forcing row 1 -> col 1 = 1) gives 11. The true
    # optimum swaps to row 0 -> col 1 (8), row 1 -> col 0 (9) = 17.
    scores = [
        [10, 8],
        [9, 1],
    ]
    assignment = assign_max_weight(scores)
    assert assignment == [1, 0]
    assert _total_score(scores, assignment) == 17


def test_assign_max_weight_square_trivial_diagonal():
    scores = [
        [5, 1],
        [1, 5],
    ]
    assignment = assign_max_weight(scores)
    assert assignment == [0, 1]


def test_assign_max_weight_more_rows_than_columns_leaves_some_unmatched():
    scores = [
        [1.0],
        [0.9],
        [0.1],
    ]
    assignment = assign_max_weight(scores)
    # Only one column exists; the best row (row 0, score 1.0) gets it.
    assert assignment == [0, None, None]


def test_assign_max_weight_more_columns_than_rows_uses_best_columns():
    scores = [
        [1.0, 0.0, 0.9],
    ]
    assignment = assign_max_weight(scores)
    assert assignment == [0]  # column 0 (1.0) beats column 2 (0.9)


def test_assign_max_weight_empty_matrix():
    assert assign_max_weight([]) == []


def test_assign_max_weight_empty_columns():
    assert assign_max_weight([[], []]) == [None, None]


def test_assign_max_weight_rectangular_optimum_matches_brute_force():
    # 3 rows x 4 cols: brute-force every injective row->col mapping and check
    # our Hungarian result achieves the same (optimal) total.
    import itertools

    scores = [
        [4, 1, 3, 2],
        [2, 0, 5, 3],
        [3, 2, 2, 1],
    ]
    best = -1.0
    for cols in itertools.permutations(range(4), 3):
        total = sum(scores[i][cols[i]] for i in range(3))
        best = max(best, total)

    assignment = assign_max_weight(scores)
    assert _total_score(scores, assignment) == best


def test_solve_min_cost_basic():
    # Minimizing cost is the dual of maximizing score; sanity-check directly.
    cost = [
        [4, 1],
        [2, 3],
    ]
    result = _solve_min_cost(cost)
    # row0->col1 (1) + row1->col0 (2) = 3, vs row0->col0(4)+row1->col1(3)=7
    assert result == [1, 0]
