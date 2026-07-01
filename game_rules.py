from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Set, Tuple


BOARD_SIZE = 19
BLACK = "black"
WHITE = "white"
EMPTY = None

Point = Tuple[int, int]
Board = List[List[Optional[str]]]


def new_board() -> Board:
    return [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def opponent(color: str) -> str:
    if color == BLACK:
        return WHITE
    if color == WHITE:
        return BLACK
    raise ValueError(f"unknown color: {color}")


def in_bounds(row: int, col: int) -> bool:
    return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE


def is_subspace_vector(dr: int, dc: int) -> bool:
    """True for non-contiguous equal-interval vectors used by this game."""
    if dr == 0 and dc == 0:
        return False
    return not (abs(dr) <= 1 and abs(dc) <= 1)


def win_vectors() -> Iterable[Point]:
    for dr in range(-4, 5):
        for dc in range(-4, 5):
            if not is_subspace_vector(dr, dc):
                continue
            if dr > 0 or (dr == 0 and dc > 0):
                yield dr, dc


def capture_vectors() -> Iterable[Point]:
    for dr in range(-6, 7):
        for dc in range(-6, 7):
            if is_subspace_vector(dr, dc):
                yield dr, dc


def find_captures(board: Board, row: int, col: int, color: str) -> Set[Point]:
    enemy = opponent(color)
    captured: Set[Point] = set()

    for dr, dc in capture_vectors():
        p1 = (row + dr, col + dc)
        p2 = (row + dr * 2, col + dc * 2)
        p3 = (row + dr * 3, col + dc * 3)
        if not all(in_bounds(*point) for point in (p1, p2, p3)):
            continue
        if (
            board[p1[0]][p1[1]] == enemy
            and board[p2[0]][p2[1]] == enemy
            and board[p3[0]][p3[1]] == color
        ):
            captured.add(p1)
            captured.add(p2)

    return captured


def remove_points(board: Board, points: Iterable[Point]) -> None:
    for row, col in points:
        board[row][col] = EMPTY


def has_five(board: Board, color: str) -> bool:
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if board[row][col] != color:
                continue
            for dr, dc in win_vectors():
                points = [(row + dr * i, col + dc * i) for i in range(5)]
                if all(in_bounds(*point) for point in points) and all(
                    board[r][c] == color for r, c in points
                ):
                    return True
    return False


def board_full(board: Board) -> bool:
    return all(cell is not EMPTY for row in board for cell in row)


def serialize_board(board: Board) -> Sequence[Sequence[Optional[str]]]:
    return board
